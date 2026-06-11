"""Regression tests for paper-trading calibration metrics."""

from core.betfair_models import (
    BetSide, OrderStyle, PaperBet, PaperBetStatus,
)
from core.paper_analysis import _calibration, _stats


def _settled_lay(*, bet_won: bool, ai_probability: float,
                 market_probability: float) -> PaperBet:
    return PaperBet(
        bet_id="lay",
        market_id="1.1",
        selection_id=1,
        side=BetSide.LAY,
        style=OrderStyle.CROSS,
        requested_odds=2.0,
        stake=10.0,
        liability=10.0,
        status=PaperBetStatus.SETTLED,
        won=bet_won,
        net_pnl=9.5 if bet_won else -10.0,
        ai_probability=ai_probability,
        market_fair_prob=market_probability,
    )


def test_lay_brier_scores_runner_outcome_not_bet_outcome():
    # Winning a LAY means the runner lost, so the actual runner outcome is 0.
    stats = _stats([
        _settled_lay(
            bet_won=True,
            ai_probability=0.10,
            market_probability=0.20,
        )
    ])
    assert abs(stats["brier"] - 0.01) < 1e-9
    assert abs(stats["market_brier"] - 0.04) < 1e-9
    assert abs(stats["brier_skill"] - 0.03) < 1e-9


def test_lay_calibration_reports_runner_loss():
    report = _calibration([
        _settled_lay(
            bet_won=True,
            ai_probability=0.10,
            market_probability=0.20,
        )
    ])
    assert "actual 0.00" in report
