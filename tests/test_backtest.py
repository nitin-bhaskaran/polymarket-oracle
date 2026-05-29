"""Tests for the backtest harness."""

from core.backtest import BacktestRow, run_backtest
from core.sizing import SizingConfig


def cfg(**kw) -> SizingConfig:
    base = dict(max_position_pct=0.10, kelly_fraction=0.25, min_trade_usd=1.0, use_kelly=True)
    base.update(kw)
    return SizingConfig(**base)


def test_skips_rows_below_min_edge():
    rows = [BacktestRow(yes_price=0.50, ai_probability=0.51, confidence=0.9, outcome=1)]
    result = run_backtest(rows, starting_capital=100, min_edge=0.05, sizing_cfg=cfg())
    assert result.n_traded == 0
    assert result.total_pnl == 0.0


def test_winning_yes_trade_makes_money():
    rows = [BacktestRow(yes_price=0.50, ai_probability=0.80, confidence=1.0, outcome=1)]
    result = run_backtest(rows, starting_capital=100, min_edge=0.05, sizing_cfg=cfg(use_kelly=False))
    assert result.n_traded == 1
    assert result.n_wins == 1
    assert result.total_pnl > 0


def test_losing_yes_trade_loses_stake():
    rows = [BacktestRow(yes_price=0.50, ai_probability=0.80, confidence=1.0, outcome=0)]
    result = run_backtest(rows, starting_capital=100, min_edge=0.05, sizing_cfg=cfg(use_kelly=False))
    assert result.n_traded == 1
    assert result.n_losses == 1
    # Lost the full stake (binary token settled at 0).
    assert result.total_pnl < 0


def test_no_edge_trade_when_spread_too_wide():
    rows = [BacktestRow(yes_price=0.50, ai_probability=0.54, confidence=1.0, outcome=1, spread=0.10)]
    result = run_backtest(rows, starting_capital=100, min_edge=0.03, sizing_cfg=cfg())
    assert result.n_traded == 0


def test_brier_score_perfect_predictions():
    rows = [
        BacktestRow(yes_price=0.50, ai_probability=1.0, confidence=1.0, outcome=1),
        BacktestRow(yes_price=0.50, ai_probability=0.0, confidence=1.0, outcome=0),
    ]
    result = run_backtest(rows, starting_capital=100, min_edge=0.05, sizing_cfg=cfg())
    assert result.brier_score == 0.0


def test_no_token_side_wins_when_resolves_no():
    # Edge says NO (ai 0.30 vs price 0.60). Resolves NO -> our NO side wins.
    rows = [BacktestRow(yes_price=0.60, ai_probability=0.30, confidence=1.0, outcome=0)]
    result = run_backtest(rows, starting_capital=100, min_edge=0.05, sizing_cfg=cfg(use_kelly=False))
    assert result.n_traded == 1
    assert result.n_wins == 1
    assert result.total_pnl > 0
