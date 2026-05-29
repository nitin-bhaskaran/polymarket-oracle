from core.models import Outcome, Side, Trade
from core.portfolio import PortfolioManager


def make_trade(**overrides) -> Trade:
    defaults = {
        "market_condition_id": "condition-1",
        "token_id": "yes-token",
        "side": Side.BUY,
        "outcome": Outcome.YES,
        "price": 0.50,
        "size": 20,
        "total_cost": 10.0,
        "success": True,
    }
    defaults.update(overrides)
    return Trade(**defaults)


def test_portfolio_persists_trades_and_closed_positions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = PortfolioManager({"risk": {"starting_capital": 100.0}})

    position = manager.record_trade(make_trade())
    manager.close_position(position, close_price=0.75, realized_pnl=5.0)

    reloaded = PortfolioManager({"risk": {"starting_capital": 100.0}})

    assert reloaded.available_capital == 105.0
    assert len(reloaded.open_positions) == 0
    assert len(reloaded.closed_positions) == 1
    assert len(reloaded.trades) == 1
    assert reloaded.closed_positions[0].current_price == 0.75


def test_daily_reset_clears_consecutive_loss_circuit_breaker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = PortfolioManager(
        {"risk": {"starting_capital": 100.0, "consecutive_loss_limit": 2}}
    )
    manager.consecutive_losses = 2
    manager._last_reset_date = None

    can_trade, reason = manager.can_trade()

    assert can_trade is True
    assert reason == "OK"
    assert manager.consecutive_losses == 0
