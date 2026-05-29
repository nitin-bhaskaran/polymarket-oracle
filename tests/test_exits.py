"""Tests for the exit decision logic."""

from core.exits import ExitConfig, ExitReason, evaluate_exit
from core.models import Outcome, Position, Side


def make_position(outcome=Outcome.YES, current_price=0.50, pnl_pct=0.0, entry=0.50) -> Position:
    pos = Position(
        market_condition_id="c1",
        question="Will it?",
        token_id="yes-token",
        side=Side.BUY,
        outcome=outcome,
        entry_price=entry,
        size=20,
        cost_basis=entry * 20,
        current_price=current_price,
    )
    pos.unrealized_pnl_pct = pnl_pct
    return pos


def cfg(**kw) -> ExitConfig:
    return ExitConfig(**kw)


def test_no_exit_when_nothing_triggers():
    pos = make_position(pnl_pct=5.0)
    assert evaluate_exit(pos, cfg()) is None


def test_stop_loss_triggers():
    pos = make_position(pnl_pct=-55.0)
    d = evaluate_exit(pos, cfg(stop_loss_pct=0.50))
    assert d is not None and d.reason == ExitReason.STOP_LOSS


def test_take_profit_triggers():
    pos = make_position(pnl_pct=60.0)
    d = evaluate_exit(pos, cfg(take_profit_pct=0.50))
    assert d is not None and d.reason == ExitReason.TAKE_PROFIT


def test_near_expiry_takes_precedence_over_everything():
    pos = make_position(pnl_pct=-90.0)  # would also stop-loss
    d = evaluate_exit(pos, cfg(), hours_to_expiry=0.5)
    assert d is not None and d.reason == ExitReason.NEAR_EXPIRY


def test_edge_closed_for_yes_position():
    # Hold YES at current price 0.70; fresh fair prob 0.71 -> edge ~0.01 < 2%.
    pos = make_position(outcome=Outcome.YES, current_price=0.70, pnl_pct=10.0)
    d = evaluate_exit(pos, cfg(edge_exit_threshold=0.02), current_fair_probability=0.71)
    assert d is not None and d.reason == ExitReason.EDGE_CLOSED


def test_edge_still_open_keeps_yes_position():
    # Fair prob 0.85 vs price 0.70 -> 15% edge remains, hold.
    pos = make_position(outcome=Outcome.YES, current_price=0.70, pnl_pct=10.0)
    assert evaluate_exit(pos, cfg(edge_exit_threshold=0.02), current_fair_probability=0.85) is None


def test_edge_closed_for_no_position_uses_complement():
    # Hold NO at price 0.60 (NO token). Fresh YES fair prob = 0.45 -> NO fair = 0.55.
    # remaining edge = 0.55 - 0.60 = -0.05 < 2% -> exit.
    pos = make_position(outcome=Outcome.NO, current_price=0.60, pnl_pct=-5.0)
    d = evaluate_exit(pos, cfg(edge_exit_threshold=0.02), current_fair_probability=0.45)
    assert d is not None and d.reason == ExitReason.EDGE_CLOSED


def test_disabled_rules_are_skipped():
    pos = make_position(pnl_pct=-90.0)
    # stop-loss disabled, nothing else triggers -> hold
    assert evaluate_exit(pos, cfg(enable_stop_loss=False, enable_take_profit=False,
                                  enable_edge_exit=False, enable_expiry_exit=False)) is None


def test_edge_rule_skipped_without_fresh_probability():
    pos = make_position(outcome=Outcome.YES, current_price=0.70, pnl_pct=2.0)
    # No fair prob passed -> edge rule can't fire; nothing else triggers.
    assert evaluate_exit(pos, cfg()) is None
