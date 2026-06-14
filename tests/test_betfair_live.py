"""Tests for fail-closed Betfair live execution."""

import json
import os
import tempfile

import pytest

from core.betfair_client import BetfairClient
from core.betfair_live import (
    LIVE_ACK, BetfairLiveTrader, LiveConfigurationError,
    validate_live_config, verify_live_app_key,
)
from core.betfair_models import (
    BetfairAssessment, BetfairMarket, BetSide, MarketPhase,
    PaperBetStatus, PriceLevel, Runner,
)
from core.paper_store import PaperBetStore


def _config(tmp, **live_overrides):
    live = {
        "enabled": True,
        "acknowledgement_env": "BETFAIR_LIVE_ACK",
        "store_path": os.path.join(tmp, "live.jsonl"),
        "audit_path": os.path.join(tmp, "attempts.jsonl"),
        "bankroll_gbp": 10.0,
        "min_stake_gbp": 1.0,
        "use_kelly_sizing": False,
        "max_liability_per_bet_gbp": 2.0,
        "max_total_liability_gbp": 3.0,
        "max_loss_gbp": 5.0,
        "max_open_bets": 2,
        "max_open_bets_per_market": 1,
        "max_bets_per_cycle": 1,
        "daily_order_limit": 2,
        "allowed_sides": ["LAY"],
        "min_edge": 0.08,
        "max_edge": 0.12,
        "min_confidence": 0.50,
        "max_confidence": 0.75,
    }
    live.update(live_overrides)
    return {
        "betfair": {"app_key_mode": "live"},
        "risk": {
            "starting_capital": 130.0,
            "max_position_pct": 10.0,
            "min_stake": 1.0,
            "use_kelly_sizing": True,
        },
        "paper": {
            "max_open_bets": 50,
            "max_total_exposure_pct": 75.0,
            "max_open_bets_per_market": 5,
            "default_sleeve": {"name": "general"},
        },
        "betfair_assessor": {"min_edge": 0.05},
        "live": live,
    }


def _market(in_play=False):
    return BetfairMarket(
        market_id="1.23",
        event_name="A v B",
        market_name="Match Odds",
        sport="MATCH_ODDS",
        in_play=in_play,
        market_version=98765,
        phase=MarketPhase.IN_PLAY if in_play else MarketPhase.PRE_EVENT,
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


def _lay_assessment():
    assessment = BetfairAssessment(
        market_id="1.23",
        selection_id=1,
        runner_name="A",
        question="?",
        estimated_probability=0.40,
        confidence=0.60,
        market_fair_prob=0.50,
        best_back=2.0,
        best_lay=2.04,
        assessment_provider="gemini",
        assessment_model="gemini-2.5-flash",
    )
    assessment.calculate_edge()
    assert assessment.recommended_side == BetSide.LAY
    return assessment


class FakeClient:
    def __init__(self, response=None, available=10.0, exposure=0.0):
        self.response = response or {
            "status": "SUCCESS",
            "instructionReports": [{
                "status": "SUCCESS",
                "betId": "123456",
                "averagePriceMatched": 2.04,
                "sizeMatched": 1.15,
                "orderStatus": "EXECUTION_COMPLETE",
            }],
        }
        self.available = available
        self.exposure = exposure
        self.calls = []
        self.fund_calls = 0

    def get_account_funds(self):
        self.fund_calls += 1
        return {
            "availableToBetBalance": self.available,
            "exposure": self.exposure,
        }

    def place_limit_order(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeScanner:
    def scan(self):
        return []

    def refresh_book(self, market_id):
        return None


def _trader(tmp, client=None, **live_overrides):
    config = _config(tmp, **live_overrides)
    store = PaperBetStore(config["live"]["store_path"])
    return BetfairLiveTrader(
        config, FakeScanner(), assessor=None,
        client=client or FakeClient(), store=store,
    )


def test_live_config_requires_all_arming_gates():
    tmp = tempfile.mkdtemp()
    config = _config(tmp)
    with pytest.raises(LiveConfigurationError, match="BETFAIR_LIVE_ACK"):
        validate_live_config(config, environ={})

    config["betfair"]["app_key_mode"] = "delayed"
    with pytest.raises(LiveConfigurationError, match="delayed key"):
        validate_live_config(
            config, environ={"BETFAIR_LIVE_ACK": LIVE_ACK}
        )


def test_live_config_accepts_explicit_ack_and_live_key():
    config = _config(tempfile.mkdtemp())
    assert validate_live_config(
        config, environ={"BETFAIR_LIVE_ACK": LIVE_ACK}
    )["bankroll_gbp"] == 10.0


def test_live_key_metadata_must_be_active_and_not_delayed():
    class KeyClient:
        app_key = "configured-key"

        def __init__(self, active=True, delayed=False):
            self.active = active
            self.delayed = delayed

        def get_developer_app_keys(self):
            return [{
                "appVersions": [{
                    "applicationKey": "configured-key",
                    "active": self.active,
                    "delayData": self.delayed,
                }],
            }]

    assert verify_live_app_key(KeyClient())["delayData"] is False
    with pytest.raises(LiveConfigurationError, match="delayed"):
        verify_live_app_key(KeyClient(delayed=True))
    with pytest.raises(LiveConfigurationError, match="not active"):
        verify_live_app_key(KeyClient(active=False))


def test_client_builds_fill_or_kill_limit_order(monkeypatch):
    client = BetfairClient({"betfair": {}})
    captured = {}

    def fake_rpc(method, params, endpoint=None):
        captured["method"] = method
        captured["params"] = params
        return {"status": "SUCCESS"}

    monkeypatch.setattr(client, "_rpc", fake_rpc)
    client.place_limit_order(
        market_id="1.1",
        selection_id=7,
        side="LAY",
        price=2.04,
        size=1.15,
        customer_ref="abc",
        customer_order_ref="order-abc",
    )

    instruction = captured["params"]["instructions"][0]
    assert captured["method"] == "SportsAPING/v1.0/placeOrders"
    assert instruction["orderType"] == "LIMIT"
    assert instruction["limitOrder"]["timeInForce"] == "FILL_OR_KILL"
    assert instruction["limitOrder"]["persistenceType"] == "LAPSE"
    assert "minFillSize" not in instruction["limitOrder"]

    client.place_limit_order(
        market_id="1.1",
        selection_id=7,
        side="LAY",
        price=2.04,
        size=1.15,
        customer_ref="def",
        customer_order_ref="order-def",
        market_version=98765,
    )
    assert captured["params"]["marketVersion"] == {"version": 98765}


def test_live_order_is_matched_recorded_and_audited():
    tmp = tempfile.mkdtemp()
    client = FakeClient()
    trader = _trader(tmp, client=client)

    bet = trader._place_paper_bet(_market(), _lay_assessment())

    assert bet is not None
    assert bet.execution_mode == "live"
    assert bet.status == PaperBetStatus.FILLED
    assert bet.exchange_bet_id == "123456"
    assert bet.liability <= 2.0
    assert len(client.calls) == 1
    assert client.calls[0]["market_version"] == 98765
    audit = [
        json.loads(line)
        for line in trader.audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert audit[0]["outcome"] == "response"
    assert audit[0]["response"]["status"] == "SUCCESS"


def test_live_rejects_in_play_before_order():
    tmp = tempfile.mkdtemp()
    client = FakeClient()
    trader = _trader(tmp, client=client)

    assert trader._place_paper_bet(
        _market(in_play=True), _lay_assessment()
    ) is None
    assert client.calls == []


def test_live_account_exposure_cap_blocks_order():
    tmp = tempfile.mkdtemp()
    client = FakeClient(exposure=3.0)
    trader = _trader(tmp, client=client)

    assert trader._place_paper_bet(_market(), _lay_assessment()) is None
    assert client.calls == []


def test_discovery_funds_check_is_cached_but_order_check_is_fresh():
    tmp = tempfile.mkdtemp()
    client = FakeClient()
    trader = _trader(tmp, client=client)
    market = _market()

    trader._exposure_capacity(market, {"name": "general"})
    trader._exposure_capacity(market, {"name": "general"})
    assert client.fund_calls == 1

    trader._place_paper_bet(market, _lay_assessment())
    assert client.fund_calls == 2


def test_live_filter_blocks_back_and_out_of_band_edges():
    tmp = tempfile.mkdtemp()
    client = FakeClient()
    trader = _trader(tmp, client=client)
    assessment = _lay_assessment()
    assessment.estimated_probability = 0.60
    assessment.calculate_edge()

    assert assessment.recommended_side == BetSide.BACK
    assert trader._place_paper_bet(_market(), assessment) is None
    assert client.calls == []
