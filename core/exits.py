"""
Exit logic — decides when an open position should be closed.

The original bot only ever closed a position on a 50% stop-loss. That leaves
two large gaps:

  * **Winners are never taken.** A position that moves your way is held until
    it either reverses into a stop-loss or the market resolves. The edge you
    identified at entry is realised only by luck of timing.
  * **Stale edges are never cut.** Once the market price moves to agree with
    the AI estimate, the reason for the position is gone — continuing to hold
    is just exposure with no thesis.

This module centralises the close decision and returns a typed reason, so the
orchestrator can act and the alerts can explain *why* a position closed. Each
rule is independently configurable and can be turned off.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.models import Outcome, Position


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    EDGE_CLOSED = "edge_closed"
    NEAR_EXPIRY = "near_expiry"


@dataclass
class ExitConfig:
    stop_loss_pct: float = 0.50        # close if unrealised loss exceeds this fraction of cost
    take_profit_pct: float = 0.50      # close if unrealised gain exceeds this fraction of cost
    edge_exit_threshold: float = 0.02  # close if remaining edge for our side falls below this
    exit_hours_before_expiry: float = 1.0  # close anything within this many hours of expiry
    enable_stop_loss: bool = True
    enable_take_profit: bool = True
    enable_edge_exit: bool = True
    enable_expiry_exit: bool = True


@dataclass
class ExitDecision:
    position: Position
    reason: ExitReason
    detail: str


def evaluate_exit(
    position: Position,
    cfg: ExitConfig,
    current_fair_probability: Optional[float] = None,
    hours_to_expiry: Optional[float] = None,
) -> Optional[ExitDecision]:
    """
    Decide whether a single position should be closed.

    Args:
        position: the open position (P&L already updated to current price).
        cfg: exit configuration.
        current_fair_probability: fresh AI probability for the YES outcome, if
            available this cycle. Used for the edge-closed rule. When None, the
            edge rule is skipped (we don't re-assess every cycle).
        hours_to_expiry: hours until the market resolves, if known.

    Returns the first matching ExitDecision, or None to keep holding.
    Order of precedence: expiry > stop-loss > take-profit > edge-closed.
    """
    # 1. Near expiry — get out before resolution mechanics lock the position.
    if cfg.enable_expiry_exit and hours_to_expiry is not None:
        if hours_to_expiry <= cfg.exit_hours_before_expiry:
            return ExitDecision(
                position, ExitReason.NEAR_EXPIRY,
                f"{hours_to_expiry:.1f}h to expiry <= {cfg.exit_hours_before_expiry:.1f}h",
            )

    # 2. Stop-loss — unrealised loss beyond threshold (pct is negative).
    if cfg.enable_stop_loss:
        if position.unrealized_pnl_pct <= -(cfg.stop_loss_pct * 100):
            return ExitDecision(
                position, ExitReason.STOP_LOSS,
                f"unrealised {position.unrealized_pnl_pct:.1f}% <= "
                f"-{cfg.stop_loss_pct * 100:.0f}%",
            )

    # 3. Take-profit — unrealised gain beyond threshold.
    if cfg.enable_take_profit:
        if position.unrealized_pnl_pct >= cfg.take_profit_pct * 100:
            return ExitDecision(
                position, ExitReason.TAKE_PROFIT,
                f"unrealised {position.unrealized_pnl_pct:.1f}% >= "
                f"+{cfg.take_profit_pct * 100:.0f}%",
            )

    # 4. Edge closed — the price has caught up to the AI's fair value, so the
    #    thesis that justified the position no longer holds.
    if cfg.enable_edge_exit and current_fair_probability is not None:
        # Fair value for the token we actually hold.
        if position.outcome == Outcome.YES:
            fair_for_held = current_fair_probability
        else:
            fair_for_held = 1.0 - current_fair_probability
        remaining_edge = fair_for_held - position.current_price
        if remaining_edge < cfg.edge_exit_threshold:
            return ExitDecision(
                position, ExitReason.EDGE_CLOSED,
                f"remaining edge {remaining_edge:+.1%} < "
                f"{cfg.edge_exit_threshold:.1%}",
            )

    return None
