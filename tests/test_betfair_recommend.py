"""Tests for read-only manually placeable recommendations."""

import json
import tempfile
from datetime import datetime, timedelta, timezone

from core.betfair_models import (
    BetfairAssessment, BetfairMarket, BetSide, MarketPhase,
    PriceLevel, Runner,
)
from core.betfair_recommend import BetfairRecommendationEngine


def _market(in_play=False):
    return BetfairMarket(
        market_id="1.50",
        event_name="A v B",
        market_name="Match Odds",
        competition="FIFA World Cup",
        sport="MATCH_ODDS",
        start_time=datetime.now(timezone.utc) + timedelta(hours=2),
        phase=MarketPhase.IN_PLAY if in_play else MarketPhase.PRE_EVENT,
        in_play=in_play,
        runners=[
            Runner(
                selection_id=1,
                name="A",
                available_to_back=[PriceLevel(price=2.0, size=100)],
                available_to_lay=[PriceLevel(price=2.04, size=100)],
            ),
            Runner(
                selection_id=2,
                name="B",
                available_to_back=[PriceLevel(price=2.0, size=100)],
                available_to_lay=[PriceLevel(price=2.04, size=100)],
            ),
        ],
    )


def _assessment(probability=0.40):
    assessment = BetfairAssessment(
        market_id="1.50",
        selection_id=1,
        runner_name="A",
        question="?",
        estimated_probability=probability,
        confidence=0.60,
        market_fair_prob=0.50,
        best_back=2.0,
        best_lay=2.04,
        reasoning="Current information modestly favours the opposing outcome.",
        assessment_provider="gemini",
        assessment_model="gemini-2.5-flash",
    )
    assessment.calculate_edge()
    return assessment


class FakeScanner:
    def __init__(self, markets, refreshed=None):
        self.markets = markets
        self.refreshed = refreshed or {
            market.market_id: market for market in markets
        }

    def scan(self):
        return self.markets

    def refresh_book(self, market_id):
        return self.refreshed.get(market_id)


class FakeSignalTrader:
    def __init__(self, assessments):
        self.assessments = assessments
        self.assess_calls = 0

    def _market_policy(self, market):
        return {"name": "general"}, ""

    def _assess(self, market):
        self.assess_calls += 1
        return self.assessments.get(market.market_id, [])

    def _odds_in_band(self, assessment):
        odds = (
            assessment.best_back
            if assessment.recommended_side == BetSide.BACK
            else assessment.best_lay
        )
        return bool(odds and 1.20 <= odds <= 21.0)


def _engine(markets, assessments, **overrides):
    tmp = tempfile.mkdtemp()
    config = {
        "betfair": {"app_key_mode": "delayed"},
        "recommendations": {
            "output_path": f"{tmp}/latest.json",
            "bankroll_gbp": 10.0,
            "min_stake_gbp": 1.0,
            "use_kelly_sizing": False,
            "max_liability_per_bet_gbp": 2.0,
            "max_total_liability_gbp": 3.0,
            "max_recommendations": 2,
            "max_markets_to_assess": 8,
            "min_hours_ahead": 0.5,
            "max_hours_ahead": 24.0,
            "valid_for_minutes": 10,
            "allowed_sides": ["LAY"],
            "min_edge": 0.08,
            "max_edge": 0.12,
            "min_confidence": 0.50,
            "max_confidence": 0.75,
            **overrides,
        },
    }
    scanner = FakeScanner(markets)
    trader = FakeSignalTrader(assessments)
    return BetfairRecommendationEngine(config, scanner, trader), trader


def test_recommendation_is_read_only_sized_and_persisted():
    market = _market()
    engine, trader = _engine(
        [market], {market.market_id: [_assessment()]}
    )

    tickets = engine.run_once()

    assert trader.assess_calls == 1
    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket.side == BetSide.LAY
    assert ticket.quoted_odds == 2.04
    assert ticket.price_rule == "LAY only at 2.04 or lower"
    assert 1.0 <= ticket.stake
    assert ticket.liability <= 2.0
    assert ticket.price_data_mode == "delayed"
    assert ticket.valid_until > ticket.generated_at
    payload = json.loads(engine.output_path.read_text(encoding="utf-8"))
    assert payload["total_recommended_liability"] <= 3.0
    assert payload["recommendations"][0]["side"] == "LAY"


def test_fresh_book_can_invalidate_old_edge():
    market = _market()
    refreshed = _market()
    refreshed.runners[0].available_to_back = [
        PriceLevel(price=2.45, size=100)
    ]
    refreshed.runners[0].available_to_lay = [
        PriceLevel(price=2.50, size=100)
    ]
    scanner = FakeScanner([market], {market.market_id: refreshed})
    trader = FakeSignalTrader({market.market_id: [_assessment()]})
    tmp = tempfile.mkdtemp()
    engine = BetfairRecommendationEngine({
        "betfair": {"app_key_mode": "delayed"},
        "recommendations": {
            "output_path": f"{tmp}/latest.json",
            "allowed_sides": ["LAY"],
            "min_edge": 0.08,
            "max_edge": 0.12,
        },
    }, scanner, trader)

    assert engine.run_once() == []


def test_in_play_market_is_never_assessed_or_recommended():
    market = _market(in_play=True)
    engine, trader = _engine(
        [market], {market.market_id: [_assessment()]}
    )

    assert engine.run_once() == []
    assert trader.assess_calls == 0


def test_total_liability_and_ticket_count_are_capped():
    first = _market()
    second = _market()
    second.market_id = "1.51"
    second.event_name = "C v D"
    second.start_time = datetime.now(timezone.utc) + timedelta(hours=3)
    second_assessment = _assessment()
    second_assessment.market_id = second.market_id
    engine, _ = _engine(
        [first, second],
        {
            first.market_id: [_assessment()],
            second.market_id: [second_assessment],
        },
        max_recommendations=2,
        max_total_liability_gbp=2.0,
    )

    tickets = engine.run_once()

    assert len(tickets) <= 2
    assert sum(ticket.liability for ticket in tickets) <= 2.0


def test_no_signal_writes_explicit_empty_result():
    market = _market()
    engine, _ = _engine(
        [market], {market.market_id: [_assessment(probability=0.49)]}
    )

    assert engine.run_once() == []
    payload = json.loads(engine.output_path.read_text(encoding="utf-8"))
    assert payload["recommendations"] == []
    assert payload["rejection_summary"]
