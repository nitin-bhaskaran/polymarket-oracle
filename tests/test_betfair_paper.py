"""Tests for the paper-bet store and paper-trading orchestrator."""

import os
import tempfile

from core.betfair_models import (
    BetfairAssessment, BetfairMarket, BetSide, MarketPhase, OrderStyle,
    PaperBet, PaperBetStatus, PriceLevel, Runner, RunnerStatus,
)
from core.paper_store import PaperBetStore
from core.betfair_paper import BetfairPaperTrader


def _store():
    tmp = tempfile.mkdtemp()
    return PaperBetStore(os.path.join(tmp, "bets.jsonl"))


def _bet(status=PaperBetStatus.FILLED, **kw):
    base = dict(bet_id="b1", market_id="1.1", selection_id=1, side=BetSide.BACK,
                style=OrderStyle.CROSS, requested_odds=2.0, stake=10, liability=10,
                status=status)
    base.update(kw)
    return PaperBet(**base)


# ── store ──

def test_store_add_and_persist():
    s = _store()
    s.add(_bet())
    # reload from disk
    s2 = PaperBetStore(str(s.path))
    assert len(s2.all()) == 1


def test_store_open_market_ids():
    s = _store()
    s.add(_bet(bet_id="a", status=PaperBetStatus.FILLED, market_id="1.1"))
    s.add(_bet(bet_id="b", status=PaperBetStatus.SETTLED, market_id="1.2"))
    assert s.open_market_ids() == {"1.1"}


def test_store_has_open_position():
    s = _store()
    s.add(_bet(bet_id="a", status=PaperBetStatus.FILLED, market_id="1.1", selection_id=5))
    assert s.has_open_position("1.1", 5)
    assert not s.has_open_position("1.1", 6)


def test_store_dedupe_against_settled_is_false():
    s = _store()
    s.add(_bet(bet_id="a", status=PaperBetStatus.SETTLED, market_id="1.1", selection_id=5))
    # settled position should NOT block a new bet
    assert not s.has_open_position("1.1", 5)


# ── orchestrator ──

def _market(in_play=False):
    return BetfairMarket(
        market_id="1.9", market_name="Match Odds", sport="Soccer",
        event_name="A v B", in_play=in_play,
        phase=MarketPhase.IN_PLAY if in_play else MarketPhase.PRE_EVENT,
        total_matched=50000,
        runners=[
            Runner(selection_id=1, name="A", status=RunnerStatus.ACTIVE,
                   available_to_back=[PriceLevel(price=2.0, size=500)],
                   available_to_lay=[PriceLevel(price=2.04, size=500)]),
            Runner(selection_id=2, name="B", status=RunnerStatus.ACTIVE,
                   available_to_back=[PriceLevel(price=2.0, size=500)],
                   available_to_lay=[PriceLevel(price=2.04, size=500)]),
        ])


class _Scanner:
    def __init__(self, market): self.m = market; self.settled = False
    def scan(self): return [self.m]
    def get_market(self, mid): return self.m
    def refresh_book(self, mid):
        if self.settled:
            m = _market()
            m.phase = MarketPhase.SETTLED
            m.runners[0].status = RunnerStatus.WINNER
            m.runners[1].status = RunnerStatus.LOSER
            return m
        return self.m


class _Assessor:
    def __init__(self, prob=0.60): self.prob = prob
    def assess_market(self, market):
        a = BetfairAssessment(market_id=market.market_id, selection_id=1, runner_name="A",
                              question="?", estimated_probability=self.prob, confidence=0.8,
                              market_fair_prob=market.fair_implied_prob(market.runners[0]),
                              best_back=2.0, best_lay=2.04, commission_rate=0.05)
        a.calculate_edge()
        return [a]


def _trader(scanner, assessor, store):
    config = {"risk": {"starting_capital": 1000, "max_position_pct": 10.0, "min_stake": 1.0},
              "betfair_assessor": {"min_edge": 0.05}, "paper": {}}
    return BetfairPaperTrader(config, scanner, assessor, store=store)


def test_cycle_places_and_fills_preevent_back():
    s = _store()
    sc = _Scanner(_market())
    t = _trader(sc, _Assessor(0.60), s)
    placed = t.run_cycle()
    assert placed == 1
    bet = s.all()[0]
    assert bet.side == BetSide.BACK
    assert bet.style == OrderStyle.CROSS
    assert bet.status == PaperBetStatus.FILLED


def test_cycle_no_bet_when_edge_below_threshold():
    s = _store()
    # AI prob ~ fair -> tiny edge
    t = _trader(_Scanner(_market()), _Assessor(0.49), s)
    assert t.run_cycle() == 0


def test_cycle_settles_on_resolution():
    s = _store()
    sc = _Scanner(_market())
    t = _trader(sc, _Assessor(0.60), s)
    t.run_cycle()
    sc.settled = True
    t.run_cycle()
    bet = s.all()[0]
    assert bet.status == PaperBetStatus.SETTLED
    assert bet.won is True
    assert bet.net_pnl > 0


def test_in_play_uses_passive_style():
    s = _store()
    m = _market(in_play=True)
    t = _trader(_Scanner(m), _Assessor(0.60), s)
    t.run_cycle()
    bet = s.all()[0]
    assert bet.style == OrderStyle.PASSIVE
    # passive doesn't fill at placement against a market that hasn't traded through
    assert bet.status in (PaperBetStatus.PENDING, PaperBetStatus.UNFILLED, PaperBetStatus.FILLED)


def test_deployed_capital_reduces_available():
    s = _store()
    t = _trader(_Scanner(_market()), _Assessor(0.60), s)
    before = t._available_capital()
    t.run_cycle()
    after = t._available_capital()
    assert after < before  # liability deployed


def test_no_duplicate_bet_same_selection():
    s = _store()
    sc = _Scanner(_market())
    t = _trader(sc, _Assessor(0.60), s)
    t.run_cycle()
    n1 = len(s.all())
    t.run_cycle()  # same market/selection still open -> no dupe
    assert len(s.all()) == n1
