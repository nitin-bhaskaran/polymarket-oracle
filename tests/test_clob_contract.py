"""
Contract tests for the py-clob-client-v2 API surface.

These guard against the single biggest risk in the trader: the code calls a
set of ClobClient methods and constructs a set of argument objects by name.
If the installed library renames or removes any of them, the bot would fall
into dry-run mode (best case) or crash on the emergency-stop path (worst case)
— and the mock-based unit tests would stay green while it happened.

The whole module is skipped when py-clob-client-v2 is not installed, so it
never blocks a credentials-light dev environment; it only runs where the real
dependency is present (CI with deps, or the deployment box).
"""

import inspect

import pytest

clob = pytest.importorskip("py_clob_client_v2")


def test_required_client_methods_exist():
    """Every ClobClient method trader.py calls must exist on the real class."""
    required = [
        "create_or_derive_api_key",   # initialize(): derive L2 creds
        "create_and_post_market_order",  # _post_market_order()
        "get_order",                  # _reconcile_fill()
        "get_trades",                 # _find_recent_order_trade()
        "get_open_orders",            # get_open_orders()
        "cancel_order",               # cancel_order()
        "cancel_all",                 # cancel_all_orders() (emergency stop)
    ]
    missing = [m for m in required if not hasattr(clob.ClobClient, m)]
    assert not missing, f"py-clob-client-v2 is missing expected methods: {missing}"


def test_required_symbols_exist():
    """Every type trader.py imports by name must exist in the package."""
    for symbol in (
        "ApiCreds",
        "ClobClient",
        "MarketOrderArgs",
        "OrderPayload",
        "OrderType",
        "PartialCreateOrderOptions",
        "Side",
        "TradeParams",
    ):
        assert hasattr(clob, symbol), f"py-clob-client-v2 missing symbol: {symbol}"


def test_market_order_args_accepts_expected_fields():
    """_post_market_order builds MarketOrderArgs with these keyword args."""
    sig = inspect.signature(clob.MarketOrderArgs.__init__)
    params = set(sig.parameters)
    for field in ("token_id", "amount", "side", "price", "order_type"):
        assert field in params, f"MarketOrderArgs is missing field: {field}"


def test_order_type_has_fok():
    """The default order type the bot uses must be available."""
    assert hasattr(clob.OrderType, "FOK")
