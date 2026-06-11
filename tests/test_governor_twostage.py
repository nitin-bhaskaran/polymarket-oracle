"""Tests for the cost-control governor and two-stage assessment routing."""

import os
import tempfile

from core.assessment_cache import AssessmentGovernor
from core.betfair_models import (
    BetfairMarket, MarketPhase, PriceLevel, Runner, RunnerStatus,
)


def _gov(**paper):
    tmp = tempfile.mkdtemp()
    cfg = {"paper": {"governor_state_path": os.path.join(tmp, "gov.json"), **paper}}
    return AssessmentGovernor(cfg)


def _market(back=2.0):
    return BetfairMarket(
        market_id="1.1", market_name="Match Odds", sport="Soccer",
        runners=[
            Runner(selection_id=1, name="A", status=RunnerStatus.ACTIVE,
                   available_to_back=[PriceLevel(price=back, size=100)]),
            Runner(selection_id=2, name="B", status=RunnerStatus.ACTIVE,
                   available_to_back=[PriceLevel(price=2.0, size=100)]),
        ])


# ── budget ──

def test_budget_starts_full():
    g = _gov(daily_deep_assessment_budget=50)
    assert g.deep_budget_remaining() == 50
    assert g.can_deep_assess()


def test_budget_decrements_and_blocks():
    g = _gov(daily_deep_assessment_budget=2)
    g.record_deep_assessment()
    assert g.deep_budget_remaining() == 1
    g.record_deep_assessment()
    assert g.deep_budget_remaining() == 0
    assert not g.can_deep_assess()


def test_budget_persists_across_instances():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "gov.json")
    cfg = {"paper": {"governor_state_path": path, "daily_deep_assessment_budget": 5}}
    g1 = AssessmentGovernor(cfg)
    g1.record_deep_assessment()
    g2 = AssessmentGovernor(cfg)
    assert g2.deep_budget_remaining() == 4


def test_paid_deep_budget_is_separate_and_persists():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "gov.json")
    cfg = {"paper": {
        "governor_state_path": path,
        "daily_deep_assessment_budget": 50,
        "daily_paid_deep_assessment_budget": 1,
    }}
    g1 = AssessmentGovernor(cfg)
    assert g1.can_paid_deep_assess()
    g1.record_paid_deep_assessment()
    assert not g1.can_paid_deep_assess()
    assert g1.deep_budget_remaining() == 50
    g2 = AssessmentGovernor(cfg)
    assert g2.paid_deep_budget_remaining() == 0


# ── change-triggered reassessment ──

def test_new_market_needs_assessment():
    g = _gov()
    assert g.needs_assessment(_market())


def test_cached_unchanged_market_skipped():
    g = _gov(reassess_after_hours=6.0, reassess_on_move=0.05)
    m = _market(back=2.0)
    g.record_assessment(m)
    # same odds, fresh -> no reassess
    assert not g.needs_assessment(_market(back=2.0))


def test_material_move_triggers_reassessment():
    g = _gov(reassess_after_hours=6.0, reassess_on_move=0.05)
    g.record_assessment(_market(back=2.0))
    # 10% move on runner A -> reassess
    assert g.needs_assessment(_market(back=2.2))


def test_small_move_does_not_trigger():
    g = _gov(reassess_after_hours=6.0, reassess_on_move=0.05)
    g.record_assessment(_market(back=2.0))
    # 2% move -> below threshold
    assert not g.needs_assessment(_market(back=2.04))


# ── two-stage routing via paper trader ──

def test_two_stage_routing_promotes_only_above_triage():
    from core.betfair_paper import BetfairPaperTrader
    from core.betfair_models import BetfairAssessment, BetSide

    class FakeTwoStage:
        triage_edge = 0.04
        def __init__(self, edge): self._edge = edge; self.deep_called = False
        def triage(self, market):
            a = BetfairAssessment(market_id=market.market_id, selection_id=1,
                                  runner_name="A", question="?",
                                  estimated_probability=0.6, confidence=0.7,
                                  market_fair_prob=0.5)
            a.calculate_edge()
            return self._edge, [a]
        def deep_assess(self, market):
            self.deep_called = True
            a = BetfairAssessment(market_id=market.market_id, selection_id=1,
                                  runner_name="A", question="?",
                                  estimated_probability=0.6, confidence=0.7,
                                  market_fair_prob=0.5, best_back=2.0)
            a.calculate_edge()
            return [a]

    class FakeScanner:
        def scan(self): return []
    cfg = {"risk": {}, "betfair_assessor": {}, "paper": {}}

    # Below triage edge -> no deep call
    g = _gov(daily_deep_assessment_budget=50)
    ts_low = FakeTwoStage(0.02)
    t = BetfairPaperTrader(cfg, FakeScanner(), assessor=None, two_stage=ts_low, governor=g,
                           store=_store())
    res = t._assess(_market())
    assert ts_low.deep_called is False
    assert res == []

    # Above triage edge -> deep call
    g2 = _gov(daily_deep_assessment_budget=50)
    ts_high = FakeTwoStage(0.10)
    t2 = BetfairPaperTrader(cfg, FakeScanner(), assessor=None, two_stage=ts_high, governor=g2,
                            store=_store())
    res2 = t2._assess(_market())
    assert ts_high.deep_called is True
    assert len(res2) == 1


def test_two_stage_respects_budget():
    from core.betfair_paper import BetfairPaperTrader
    from core.betfair_models import BetfairAssessment

    class FakeTwoStage:
        triage_edge = 0.04
        def triage(self, market):
            a = BetfairAssessment(market_id=market.market_id, selection_id=1,
                                  runner_name="A", question="?",
                                  estimated_probability=0.6, confidence=0.7,
                                  market_fair_prob=0.5)
            a.calculate_edge()
            return 0.10, [a]
        def deep_assess(self, market):
            raise AssertionError("should not deep-assess when budget exhausted")

    class FakeScanner:
        def scan(self): return []
    cfg = {"risk": {}, "betfair_assessor": {}, "paper": {}}
    g = _gov(daily_deep_assessment_budget=0)  # no budget
    t = BetfairPaperTrader(cfg, FakeScanner(), assessor=None,
                           two_stage=FakeTwoStage(), governor=g, store=_store())
    # triage flags edge, but budget is 0 -> returns [] without deep
    assert t._assess(_market()) == []


def _store():
    from core.paper_store import PaperBetStore
    tmp = tempfile.mkdtemp()
    return PaperBetStore(os.path.join(tmp, "bets.jsonl"))


# ── triage-edge upper guard + extreme-odds bet filter ──

def test_triage_confusion_cap_skips_deep():
    """An absurd triage edge (near-certain market) is skipped, not deep-assessed."""
    from core.betfair_paper import BetfairPaperTrader
    from core.betfair_models import BetfairAssessment

    class FakeTwoStage:
        triage_edge = 0.04
        def triage(self, market):
            a = BetfairAssessment(market_id=market.market_id, selection_id=1,
                                  runner_name="Yes", question="?",
                                  estimated_probability=0.84, confidence=0.5,
                                  market_fair_prob=0.003)
            a.calculate_edge()
            return 0.84, [a]  # 84% triage edge = confusion
        def deep_assess(self, market):
            raise AssertionError("should not deep-assess a triage-confused market")

    class FakeScanner:
        def scan(self): return []
    cfg = {"risk": {}, "betfair_assessor": {"max_triage_edge": 0.40}, "paper": {}}
    g = _gov(daily_deep_assessment_budget=50)
    t = BetfairPaperTrader(cfg, FakeScanner(), assessor=None,
                           two_stage=FakeTwoStage(), governor=g, store=_store())
    assert t._assess(_market()) == []


def test_extreme_odds_assessments_filtered():
    """Deep assessments on near-lock / longshot odds are dropped before placement."""
    from core.betfair_paper import BetfairPaperTrader
    from core.betfair_models import BetfairAssessment, BetSide

    class FakeTwoStage:
        triage_edge = 0.04
        def triage(self, market):
            a = BetfairAssessment(market_id=market.market_id, selection_id=1,
                                  runner_name="A", question="?",
                                  estimated_probability=0.6, confidence=0.7,
                                  market_fair_prob=0.5)
            a.calculate_edge()
            return 0.10, [a]
        def deep_assess(self, market):
            # Two assessments: one tradeable (odds 2.0), one near-lock (odds 1.05)
            good = BetfairAssessment(market_id=market.market_id, selection_id=1,
                                     runner_name="A", question="?",
                                     estimated_probability=0.6, confidence=0.7,
                                     market_fair_prob=0.5, best_back=2.0)
            good.calculate_edge()
            lock = BetfairAssessment(market_id=market.market_id, selection_id=2,
                                     runner_name="B", question="?",
                                     estimated_probability=0.99, confidence=0.7,
                                     market_fair_prob=0.95, best_back=1.05)
            lock.calculate_edge()
            return [good, lock]

    class FakeScanner:
        def scan(self): return []
    cfg = {"risk": {}, "betfair_assessor": {"min_odds": 1.20, "max_odds": 21.0}, "paper": {}}
    g = _gov(daily_deep_assessment_budget=50)
    t = BetfairPaperTrader(cfg, FakeScanner(), assessor=None,
                           two_stage=FakeTwoStage(), governor=g, store=_store())
    res = t._assess(_market())
    # Only the tradeable-odds assessment survives
    assert len(res) == 1
    assert res[0].best_back == 2.0


# ── favourite-floor coherence guard ──

def _twostage_returning(assessments):
    from core.betfair_models import BetfairAssessment
    class FakeTwoStage:
        triage_edge = 0.04
        def triage(self, market):
            return 0.10, []
        def deep_assess(self, market):
            return assessments
    return FakeTwoStage()


def _assessment(sel, name, ai_p, fair_p, back):
    from core.betfair_models import BetfairAssessment
    a = BetfairAssessment(market_id="1.1", selection_id=sel, runner_name=name,
                          question="?", estimated_probability=ai_p, confidence=0.7,
                          market_fair_prob=fair_p, best_back=back, best_lay=back + 0.1)
    a.calculate_edge()
    return a


def test_favourite_floor_rejects_broken_distribution():
    """Favourite (market 32%) assessed at 1% -> whole market rejected."""
    from core.betfair_paper import BetfairPaperTrader
    class FakeScanner:
        def scan(self): return []
    cfg = {"risk": {}, "betfair_assessor": {"favourite_floor_fraction": 0.5},
           "paper": {}}
    g = _gov(daily_deep_assessment_budget=50)
    # Sabalenka-like: favourite fair 32%, AI 1%; plus inflated mid-runners
    assessments = [
        _assessment(1, "Sabalenka", 0.01, 0.32, 3.45),
        _assessment(2, "Gauff", 0.22, 0.07, 6.2),
        _assessment(3, "Andreeva", 0.20, 0.09, 10.0),
    ]
    t = BetfairPaperTrader(cfg, FakeScanner(), assessor=None,
                           two_stage=_twostage_returning(assessments),
                           governor=g, store=_store())
    assert t._assess(_market()) == []


def test_favourite_floor_keeps_plausible_distribution():
    """Favourite assessed near its market prob -> market kept, edges flow through."""
    from core.betfair_paper import BetfairPaperTrader
    class FakeScanner:
        def scan(self): return []
    cfg = {"risk": {}, "betfair_assessor": {"favourite_floor_fraction": 0.5,
                                            "min_odds": 1.20, "max_odds": 21.0},
           "paper": {}}
    g = _gov(daily_deep_assessment_budget=50)
    # Favourite fair 50%, AI 46% (well above floor of 25%) -> kept
    assessments = [
        _assessment(1, "A", 0.46, 0.50, 2.0),
        _assessment(2, "B", 0.30, 0.30, 3.4),
        _assessment(3, "C", 0.24, 0.20, 5.0),
    ]
    t = BetfairPaperTrader(cfg, FakeScanner(), assessor=None,
                           two_stage=_twostage_returning(assessments),
                           governor=g, store=_store())
    res = t._assess(_market())
    assert len(res) == 3  # all within odds band, market not rejected


def test_favourite_floor_disabled_when_zero():
    from core.betfair_paper import BetfairPaperTrader
    class FakeScanner:
        def scan(self): return []
    cfg = {"risk": {}, "betfair_assessor": {"favourite_floor_fraction": 0.0,
                                            "min_odds": 1.20, "max_odds": 21.0},
           "paper": {}}
    g = _gov(daily_deep_assessment_budget=50)
    assessments = [
        _assessment(1, "Sabalenka", 0.01, 0.32, 3.45),
        _assessment(2, "Gauff", 0.22, 0.07, 6.2),
    ]
    t = BetfairPaperTrader(cfg, FakeScanner(), assessor=None,
                           two_stage=_twostage_returning(assessments),
                           governor=g, store=_store())
    # floor disabled -> not rejected on favourite grounds (odds band still applies)
    assert len(t._assess(_market())) == 2


def test_deep_spacing_sleeps_when_recent(monkeypatch):
    """If a deep assessment ran recently, the next one waits."""
    import core.betfair_paper as bp
    from core.betfair_paper import BetfairPaperTrader
    class FakeScanner:
        def scan(self): return []
    cfg = {"risk": {}, "betfair_assessor": {"favourite_floor_fraction": 0.0,
                                            "min_odds": 1.20, "max_odds": 21.0},
           "paper": {"deep_min_interval_seconds": 20.0}}
    g = _gov(daily_deep_assessment_budget=50)
    t = BetfairPaperTrader(cfg, FakeScanner(), assessor=None,
                           two_stage=_twostage_returning([_assessment(1, "A", 0.6, 0.5, 2.0)]),
                           governor=g, store=_store())
    slept = {}
    monkeypatch.setattr(bp.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(bp.time, "sleep", lambda s: slept.setdefault("s", s))
    t._last_deep_at = 995.0  # 5s ago, need 20s gap -> should sleep ~15s
    t._assess(_market())
    assert slept.get("s", 0) > 10
