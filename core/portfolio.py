"""
Portfolio Manager — Tracks positions, P&L, and risk limits.

Maintains state of all open and closed positions, calculates
real-time P&L, and enforces risk management rules.
"""

import json
import logging
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

from core.models import (
    Market, Outcome, Position, PortfolioSnapshot, Side, Trade
)
from core.money import dec, usdc
from core.exits import ExitConfig, ExitDecision, evaluate_exit

logger = logging.getLogger(__name__)


class PortfolioManager:
    """
    Manages the trading portfolio — positions, capital, and risk.

    Persists state to a JSON file so it survives restarts.
    """

    def __init__(self, config: dict):
        risk_config = config.get("risk", {})

        self.starting_capital = risk_config.get("starting_capital", 130.0)
        self.max_positions = risk_config.get("max_positions", 10)
        self.max_position_pct = risk_config.get("max_position_pct", 10.0) / 100
        self.stop_loss_pct = risk_config.get("stop_loss_pct", 50.0) / 100
        self.daily_loss_limit_pct = risk_config.get("daily_loss_limit_pct", 15.0) / 100
        self.consecutive_loss_limit = risk_config.get("consecutive_loss_limit", 5)

        # Exit rules (stop-loss, take-profit, edge-closed, near-expiry)
        self.exit_config = ExitConfig(
            stop_loss_pct=self.stop_loss_pct,
            take_profit_pct=risk_config.get("take_profit_pct", 50.0) / 100,
            edge_exit_threshold=risk_config.get("edge_exit_threshold_pct", 2.0) / 100,
            exit_hours_before_expiry=risk_config.get("exit_hours_before_expiry", 1.0),
            enable_stop_loss=risk_config.get("enable_stop_loss", True),
            enable_take_profit=risk_config.get("enable_take_profit", True),
            enable_edge_exit=risk_config.get("enable_edge_exit", True),
            enable_expiry_exit=risk_config.get("enable_expiry_exit", True),
        )

        # State
        self.available_capital = self.starting_capital
        self.open_positions: list[Position] = []
        self.closed_positions: list[Position] = []
        self.trades: list[Trade] = []
        self.daily_pnl: float = 0.0
        self.consecutive_losses: int = 0
        self._last_reset_date: Optional[date] = None

        # State file path
        self.state_file = Path("data/portfolio_state.json")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        # Load existing state if available
        self._load_state()

    def can_trade(self) -> tuple[bool, str]:
        """
        Check if we're allowed to open new positions.

        Returns (allowed, reason) tuple.
        """
        # Reset daily counters if new day
        self._check_daily_reset()

        # Check position count
        if len(self.open_positions) >= self.max_positions:
            return False, f"Max positions reached ({self.max_positions})"

        # Check daily loss limit
        daily_limit = self.starting_capital * self.daily_loss_limit_pct
        if self.daily_pnl < -daily_limit:
            return False, f"Daily loss limit hit (${self.daily_pnl:.2f})"

        # Check consecutive losses
        if self.consecutive_losses >= self.consecutive_loss_limit:
            return False, f"Circuit breaker: {self.consecutive_losses} consecutive losses"

        # Check available capital
        min_trade = self.starting_capital * self.max_position_pct * 0.5
        if self.available_capital < min_trade:
            return False, f"Insufficient capital (${self.available_capital:.2f})"

        return True, "OK"

    def record_trade(self, trade: Trade) -> Optional[Position]:
        """
        Record a new trade and create/update the corresponding position.

        Returns the created Position if trade was successful.
        """
        self.trades.append(trade)

        if not trade.success:
            logger.warning(f"Recording failed trade: {trade.error_message}")
            return None

        if dec(trade.total_cost) > dec(self.available_capital):
            logger.error(
                f"Rejecting trade that exceeds available capital: "
                f"${trade.total_cost:.2f} > ${self.available_capital:.2f}"
            )
            return None

        # Create new position
        position = Position(
            market_condition_id=trade.market_condition_id,
            question="",  # Will be filled from market context
            token_id=trade.token_id,
            side=trade.side,
            outcome=trade.outcome,
            entry_price=trade.price,
            size=trade.size,
            cost_basis=trade.total_cost,
            current_price=trade.price,
            current_value=trade.total_cost,
            edge_at_entry=trade.edge_at_trade,
            ai_probability_at_entry=trade.ai_probability,
        )

        # Update capital
        self.available_capital = usdc(dec(self.available_capital) - dec(trade.total_cost))
        self.open_positions.append(position)

        logger.info(
            f"Position opened: {trade.outcome.value} @ ${trade.price:.3f} | "
            f"Cost: ${trade.total_cost:.2f} | "
            f"Available capital: ${self.available_capital:.2f}"
        )

        self._save_state()
        return position

    def update_positions(self, price_lookup: dict[str, float]):
        """
        Update all open positions with current prices.

        price_lookup: dict mapping token_id -> current_price
        """
        for position in self.open_positions:
            if position.token_id in price_lookup:
                position.update_pnl(price_lookup[position.token_id])

    def positions_to_close(
        self,
        fair_probabilities: Optional[dict[str, float]] = None,
        hours_to_expiry: Optional[dict[str, float]] = None,
    ) -> list[ExitDecision]:
        """
        Evaluate every open position against all exit rules.

        Args:
            fair_probabilities: optional map of market_condition_id -> fresh AI
                YES-probability for this cycle, used by the edge-closed rule.
            hours_to_expiry: optional map of market_condition_id -> hours until
                the market resolves, used by the near-expiry rule.

        Returns a list of ExitDecision for positions that should be closed.
        """
        fair_probabilities = fair_probabilities or {}
        hours_to_expiry = hours_to_expiry or {}
        decisions: list[ExitDecision] = []

        for position in self.open_positions:
            decision = evaluate_exit(
                position,
                self.exit_config,
                current_fair_probability=fair_probabilities.get(
                    position.market_condition_id
                ),
                hours_to_expiry=hours_to_expiry.get(position.market_condition_id),
            )
            if decision:
                logger.warning(
                    f"Exit ({decision.reason.value}) for "
                    f"{position.market_condition_id}: {decision.detail}"
                )
                decisions.append(decision)

        return decisions

    def check_stop_losses(self) -> list[Position]:
        """
        Backward-compatible helper: positions hitting the stop-loss rule only.

        Prefer positions_to_close(), which also covers take-profit, edge-closed,
        and near-expiry exits.
        """
        return [
            d.position
            for d in self.positions_to_close()
            if d.reason.value == "stop_loss"
        ]

    def close_position(self, position: Position, close_price: float, realized_pnl: float):
        """Mark a position as closed and update accounting."""
        position.is_open = False
        position.closed_at = datetime.now(timezone.utc)
        position.current_price = close_price

        # Update P&L tracking
        self.daily_pnl = usdc(dec(self.daily_pnl) + dec(realized_pnl))
        self.available_capital = usdc(
            dec(self.available_capital) + dec(position.cost_basis) + dec(realized_pnl)
        )

        # Track consecutive wins/losses
        if realized_pnl >= 0:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1

        # Move to closed positions
        self.open_positions.remove(position)
        self.closed_positions.append(position)

        logger.info(
            f"Position closed: PnL ${realized_pnl:.2f} | "
            f"Available capital: ${self.available_capital:.2f}"
        )

        self._save_state()

    def get_snapshot(self) -> PortfolioSnapshot:
        """Generate a point-in-time portfolio snapshot."""
        deployed = usdc(sum(dec(p.cost_basis) for p in self.open_positions))
        unrealized = usdc(sum(dec(p.unrealized_pnl) for p in self.open_positions))
        realized = usdc(
            sum(dec(p.current_value) - dec(p.cost_basis) for p in self.closed_positions)
        )

        winning = sum(
            1 for t in self.trades
            if t.success and t.realized_pnl is not None and t.realized_pnl > 0
        )
        losing = sum(
            1 for t in self.trades
            if t.success and t.realized_pnl is not None and t.realized_pnl < 0
        )

        return PortfolioSnapshot(
            total_capital=self.starting_capital,
            available_capital=self.available_capital,
            deployed_capital=deployed,
            total_unrealized_pnl=unrealized,
            total_realized_pnl=realized,
            daily_pnl=self.daily_pnl,
            open_positions=len(self.open_positions),
            total_trades=len([t for t in self.trades if t.success]),
            winning_trades=winning,
            losing_trades=losing,
        )

    def _check_daily_reset(self):
        """Reset daily counters at the start of each new day."""
        today = date.today()
        if self._last_reset_date != today:
            logger.info(f"New day ({today}) — resetting daily P&L counter")
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            self._last_reset_date = today

    def _save_state(self):
        """Persist portfolio state to JSON file."""
        try:
            state = {
                "available_capital": self.available_capital,
                "daily_pnl": self.daily_pnl,
                "consecutive_losses": self.consecutive_losses,
                "last_reset_date": str(self._last_reset_date) if self._last_reset_date else None,
                "open_positions": [p.model_dump(mode="json") for p in self.open_positions],
                "closed_positions": [p.model_dump(mode="json") for p in self.closed_positions],
                "trades": [t.model_dump(mode="json") for t in self.trades],
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }

            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2, default=str)

        except Exception as e:
            logger.error(f"Failed to save portfolio state: {e}")

    def _load_state(self):
        """Load portfolio state from JSON file if it exists."""
        try:
            if self.state_file.exists():
                with open(self.state_file) as f:
                    state = json.load(f)

                self.available_capital = state.get("available_capital", self.starting_capital)
                self.daily_pnl = state.get("daily_pnl", 0.0)
                self.consecutive_losses = state.get("consecutive_losses", 0)

                last_date = state.get("last_reset_date")
                if last_date:
                    self._last_reset_date = date.fromisoformat(last_date)

                # Reconstruct open positions
                for pos_data in state.get("open_positions", []):
                    try:
                        position = Position(**pos_data)
                        self.open_positions.append(position)
                    except Exception as e:
                        logger.warning(f"Failed to load position: {e}")

                for pos_data in state.get("closed_positions", []):
                    try:
                        position = Position(**pos_data)
                        self.closed_positions.append(position)
                    except Exception as e:
                        logger.warning(f"Failed to load closed position: {e}")

                for trade_data in state.get("trades", []):
                    try:
                        trade = Trade(**trade_data)
                        self.trades.append(trade)
                    except Exception as e:
                        logger.warning(f"Failed to load trade: {e}")

                logger.info(
                    f"Loaded portfolio state: "
                    f"${self.available_capital:.2f} available, "
                    f"{len(self.open_positions)} open positions"
                )
            else:
                logger.info("No existing portfolio state — starting fresh")

        except Exception as e:
            logger.error(f"Failed to load portfolio state: {e}")
            logger.info("Starting with fresh state")
