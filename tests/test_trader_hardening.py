"""
Tests for trader hardening fixes:
- cancel_all_orders() emergency-stop batch behaviour (real API has no cancel_all)
- _price_limit() slippage + near-bound clamping
- _normalize_tick_size() validation against the CLOB-allowed set
"""

from core.models import Side as TradeSide
from core.trader import Trader


def _trader(**poly):
    cfg = {"polymarket": {"private_key": "YOUR_PRIVATE_KEY_HERE", **poly}}
    t = Trader(cfg)
    t.initialize()  # falls into dry-run; no real client
    return t


# ── cancel_all_orders ───────────────────────────────────────────────

class _RecordingClient:
    def __init__(self):
        self.cancel_all_called = False

    def cancel_all(self):
        self.cancel_all_called = True
        return {"cancelled": "all"}


def test_cancel_all_orders_calls_cancel_all():
    client = _RecordingClient()
    t = _trader()
    t.client = client
    t._initialized = True

    assert t.cancel_all_orders() is True
    assert client.cancel_all_called is True


def test_cancel_all_orders_false_when_not_initialized():
    t = _trader()
    t.client = None
    t._initialized = False
    assert t.cancel_all_orders() is False


def test_cancel_all_orders_false_on_client_error():
    class Boom:
        def cancel_all(self):
            raise RuntimeError("network down")

    t = _trader()
    t.client = Boom()
    t._initialized = True
    assert t.cancel_all_orders() is False


# ── _price_limit ────────────────────────────────────────────────────

def test_price_limit_buy_adds_slippage():
    t = _trader()
    t.slippage_bps = 150  # 1.5%
    t.tick_size = "0.01"
    # 0.50 * 1.015 = 0.5075 -> quantized to 4dp
    assert t._price_limit(0.50, TradeSide.BUY) == 0.5075


def test_price_limit_sell_subtracts_slippage():
    t = _trader()
    t.slippage_bps = 150
    t.tick_size = "0.01"
    # 0.50 * 0.985 = 0.4925
    assert t._price_limit(0.50, TradeSide.SELL) == 0.4925


def test_price_limit_buy_clamps_one_tick_below_one():
    t = _trader()
    t.slippage_bps = 1000  # 10%, would push a 0.97 market over 1.0
    t.tick_size = "0.01"
    # ceiling = 1 - 0.01 = 0.99
    assert t._price_limit(0.97, TradeSide.BUY) == 0.99


def test_price_limit_sell_clamps_one_tick_above_zero():
    t = _trader()
    t.slippage_bps = 5000  # 50%, pushes a 0.015 market below the 0.01 floor
    t.tick_size = "0.01"
    # 0.015 * 0.5 = 0.0075, below floor 0.01 -> clamped to 0.01
    assert t._price_limit(0.015, TradeSide.SELL) == 0.01


# ── _normalize_tick_size ────────────────────────────────────────────

def test_tick_size_valid_values_preserved():
    for v in ("0.1", "0.01", "0.001", "0.0001"):
        assert Trader._normalize_tick_size(v) == v


def test_tick_size_invalid_falls_back_to_default():
    assert Trader._normalize_tick_size("0.5") == "0.01"
    assert Trader._normalize_tick_size("garbage") == "0.01"
    assert Trader._normalize_tick_size(0.01) == "0.01"  # numeric coerced to str
