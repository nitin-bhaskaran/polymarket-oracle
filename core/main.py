"""
Polymarket Oracle — Main entry point and orchestrator.

Runs the continuous scan → assess → trade → monitor loop.
Coordinates the market scanner, probability engine, trader,
portfolio manager, and Telegram alerts.
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from core.market_scanner import MarketScanner

logger = logging.getLogger("oracle")


def setup_logging(config: dict):
    """Configure logging based on config settings."""
    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO").upper())
    log_file = log_config.get("file", "logs/oracle.log")

    # Create log directory
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    # Force UTF-8 on stdout so emoji in log messages don't crash the handler
    # on Windows consoles (default cp1252 can't encode them).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    # Configure root logger
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ]
    )


def load_config(config_path: str = "config/config.yaml") -> dict:
    """Load configuration from YAML file."""
    try:
        if load_dotenv:
            load_dotenv()
        with open(config_path) as f:
            config = yaml.safe_load(f)
        _apply_env_overrides(config)
        logger.info(f"Configuration loaded from {config_path}")
        return config
    except FileNotFoundError:
        logger.error(
            f"Config file not found: {config_path}\n"
            f"Copy config/config.example.yaml to config/config.yaml "
            f"and fill in your credentials."
        )
        sys.exit(1)


def _apply_env_overrides(config: dict):
    """Allow environment variables to override secret-bearing config values."""
    poly_config = config.setdefault("polymarket", {})
    anthropic_config = config.setdefault("anthropic", {})
    telegram_config = config.setdefault("telegram", {})

    mappings = [
        ("POLYMARKET_PK", poly_config, "private_key"),
        ("POLYMARKET_FUNDER", poly_config, "funder_address"),
        ("POLYMARKET_CLOB_API_KEY", poly_config, "clob_api_key"),
        ("POLYMARKET_CLOB_API_SECRET", poly_config, "clob_api_secret"),
        ("POLYMARKET_CLOB_API_PASSPHRASE", poly_config, "clob_api_passphrase"),
        ("ANTHROPIC_API_KEY", anthropic_config, "api_key"),
        ("TELEGRAM_BOT_TOKEN", telegram_config, "bot_token"),
        ("TELEGRAM_CHAT_ID", telegram_config, "chat_id"),
    ]
    for env_name, section, key in mappings:
        value = os.getenv(env_name)
        if value:
            section[key] = value


class Oracle:
    """
    Main orchestrator that runs the trading loop.

    The loop runs on a configurable interval (default: 5 minutes):

    1. SCAN   — Fetch active markets, apply filters
    2. ASSESS — Use Claude API to estimate probabilities
    3. TRADE  — Execute trades where edge exceeds threshold
    4. MONITOR — Update positions, check stop-losses
    5. REPORT — Send Telegram alerts
    6. SLEEP  — Wait for next cycle
    """

    def __init__(self, config: dict, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.running = False
        self.scan_interval = config.get("scanner", {}).get("scan_interval", 300)

        # Initialize components
        logger.info("Initializing Oracle components...")
        from core.probability import ProbabilityEngine
        from core.trader import Trader
        from core.portfolio import PortfolioManager
        from alerts.telegram import TelegramAlerts

        self.scanner = MarketScanner(config)
        self.probability = ProbabilityEngine(config)
        self.trader = Trader(config)
        self.portfolio = PortfolioManager(config)
        self.alerts = TelegramAlerts(config)
        self._telegram_app = None
        self._telegram_thread = None

        if dry_run:
            self.trader.dry_run = True
            logger.info("🔸 Running in DRY RUN mode — no real trades will be executed")

        # Initialize trader (derives API keys)
        self.trader.initialize()
        self._start_telegram_commands()

        logger.info("All components initialized")

    def run(self):
        """Start the main trading loop."""
        self.running = True

        # Send startup alert
        self.alerts.alert_startup()

        logger.info(
            f"Oracle started | "
            f"Capital: ${self.portfolio.available_capital:.2f} | "
            f"Scan interval: {self.scan_interval}s | "
            f"Dry run: {self.dry_run}"
        )

        cycle_count = 0

        while self.running:
            cycle_count += 1
            cycle_start = time.time()

            try:
                logger.info(f"{'='*60}")
                logger.info(f"Cycle #{cycle_count}")
                logger.info(f"{'='*60}")

                # Check if trading is paused (via Telegram command)
                if self.alerts.trading_paused:
                    logger.info("Trading paused — skipping cycle")
                    time.sleep(self.scan_interval)
                    continue

                # Check if we can trade
                can_trade, reason = self.portfolio.can_trade()
                if not can_trade:
                    logger.warning(f"Cannot trade: {reason}")
                    self.alerts.alert_circuit_breaker(reason)
                    time.sleep(self.scan_interval)
                    continue

                # ── STEP 1: SCAN ──
                logger.info("Step 1: Scanning markets...")
                markets = self.scanner.scan()

                if not markets:
                    logger.info("No tradeable markets found this cycle")
                    time.sleep(self.scan_interval)
                    continue

                # ── STEP 2: ASSESS ──
                logger.info(f"Step 2: Assessing {len(markets)} markets...")
                assessments = self.probability.batch_assess(markets)

                # Map condition_id -> fresh YES probability, for the edge-closed
                # exit rule on any positions we hold in those markets.
                fair_probabilities = {
                    a.market_condition_id: a.estimated_probability
                    for a in assessments
                }

                # Filter for actionable edge
                min_edge = self.config.get("risk", {}).get("min_edge", 5.0) / 100
                actionable = [a for a in assessments if a.abs_edge >= min_edge]

                if actionable:
                    logger.info(f"Found {len(actionable)} markets with edge >= {min_edge:.0%}")
                    for a in actionable:
                        self.alerts.alert_edge_detected(a)
                else:
                    logger.info("No actionable edge found this cycle")

                # ── STEP 3: TRADE ──
                logger.info(f"Step 3: Executing trades...")
                for assessment in actionable:
                    # Find the corresponding market
                    market = next(
                        (m for m in markets if m.condition_id == assessment.market_condition_id),
                        None
                    )
                    if not market:
                        continue

                    # Check we can still trade (might hit limits mid-cycle)
                    can_trade, reason = self.portfolio.can_trade()
                    if not can_trade:
                        logger.warning(f"Stopping trades: {reason}")
                        break

                    # Fetch the live spread for the token we'd actually buy, so
                    # sizing accounts for the cost of crossing it. Fall back to a
                    # conservative default if the spread fetch fails (never 0).
                    buy_token = (
                        market.yes_token_id if assessment.edge > 0
                        else market.no_token_id
                    )
                    spread = self.scanner.get_spread(buy_token)
                    if spread is None:
                        spread = self.config.get("risk", {}).get(
                            "default_spread_pct", 2.0
                        ) / 100

                    # Execute
                    trade = self.trader.execute_trade(
                        market=market,
                        assessment=assessment,
                        available_capital=self.portfolio.available_capital,
                        spread=spread,
                    )

                    if trade:
                        position = self.portfolio.record_trade(trade)
                        self.alerts.alert_trade_executed(trade)

                # ── STEP 4: MONITOR ──
                logger.info("Step 4: Monitoring positions...")
                self._monitor_positions(fair_probabilities=fair_probabilities)

                # ── STEP 5: REPORT ──
                # Send daily summary at end of each cycle
                if cycle_count % 12 == 0:  # Every ~1 hour (12 x 5 min)
                    snapshot = self.portfolio.get_snapshot()
                    self.alerts.alert_daily_summary(snapshot)

                # Heartbeat — write a small health file every cycle so external
                # monitoring (or you) can confirm the bot is alive and see its
                # last-known state without parsing logs.
                self._write_heartbeat(cycle_count)

                # Log cycle time
                elapsed = time.time() - cycle_start
                logger.info(f"Cycle #{cycle_count} completed in {elapsed:.1f}s")

            except KeyboardInterrupt:
                logger.info("Shutting down...")
                self.running = False
                break
            except Exception as e:
                logger.error(f"Error in cycle #{cycle_count}: {e}", exc_info=True)
                self.alerts.alert_error(str(e))

            # ── STEP 6: SLEEP ──
            if self.running:
                logger.info(f"Sleeping {self.scan_interval}s until next cycle...")
                time.sleep(self.scan_interval)

        self._shutdown()

    def _start_telegram_commands(self):
        """
        Start Telegram command polling in the background when configured.

        The poller runs in a daemon thread. Because the /stop emergency control
        depends on this thread being alive, we supervise it: if run_polling()
        ever exits while the bot is still running, we rebuild the Application
        and restart polling after a short backoff, and alert on each restart so
        a persistently failing control surface is visible.
        """
        from alerts.telegram import setup_command_handlers

        def build_app():
            return setup_command_handlers(
                alerts=self.alerts,
                get_snapshot_fn=self.portfolio.get_snapshot,
                get_positions_fn=lambda: self.portfolio.open_positions,
                pause_fn=lambda: logger.info("Trading paused via Telegram"),
                resume_fn=lambda: logger.info("Trading resumed via Telegram"),
                emergency_stop_fn=self.trader.cancel_all_orders,
            )

        # Probe once so we can no-op cleanly when Telegram isn't configured.
        self._telegram_app = build_app()
        if not self._telegram_app:
            return

        def supervise():
            backoff = 5
            max_backoff = 300
            first = True
            while self.running or first:
                first = False
                try:
                    if self._telegram_app is None:
                        self._telegram_app = build_app()
                    if self._telegram_app is None:
                        return
                    self._telegram_app.run_polling(stop_signals=None)
                    # run_polling returned without raising — normal stop.
                    if not self.running:
                        return
                    logger.warning("Telegram polling exited unexpectedly; restarting")
                except Exception as e:
                    logger.error(f"Telegram command polling crashed: {e}", exc_info=True)
                    try:
                        self.alerts.alert_error(
                            f"Telegram command polling crashed and is restarting: {e}"
                        )
                    except Exception:
                        pass

                # Force a fresh Application on the next loop; a stopped PTB
                # Application cannot be restarted in place.
                self._telegram_app = None
                if not self.running:
                    return
                time.sleep(backoff)
                backoff = min(max_backoff, backoff * 2)

        self._telegram_thread = threading.Thread(
            target=supervise,
            name="telegram-command-polling",
            daemon=True,
        )
        self._telegram_thread.start()
        logger.info("Telegram command handlers started (supervised)")

    def _write_heartbeat(self, cycle_count: int):
        """
        Write a small JSON health file with the bot's last-known state.

        External monitoring can check the file's mtime to confirm the bot is
        alive and read the contents for a quick state summary. Failures here
        are non-fatal — a heartbeat write must never crash the trading loop.
        """
        try:
            snapshot = self.portfolio.get_snapshot()
            health = {
                "last_cycle_at": datetime.now(timezone.utc).isoformat(),
                "cycle_count": cycle_count,
                "running": self.running,
                "dry_run": self.dry_run,
                "trading_paused": self.alerts.trading_paused,
                "available_capital": snapshot.available_capital,
                "total_value": snapshot.total_value,
                "open_positions": snapshot.open_positions,
                "daily_pnl": snapshot.daily_pnl,
            }
            path = Path("data/health.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(health, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"Failed to write heartbeat: {e}")

    def _monitor_positions(self, fair_probabilities: dict = None):
        """
        Update positions and evaluate all exit rules.

        Args:
            fair_probabilities: optional map of market_condition_id -> fresh AI
                YES-probability from this cycle's assessments, used for the
                edge-closed exit rule.
        """
        if not self.portfolio.open_positions:
            return

        # Build price lookup and expiry lookup from the scanner.
        price_lookup = {}
        hours_to_expiry = {}
        for position in self.portfolio.open_positions:
            price = self.scanner.get_current_price(position.token_id)
            if price is not None:
                price_lookup[position.token_id] = price
            market = self.scanner.get_market(position.market_condition_id) \
                if hasattr(self.scanner, "get_market") else None
            if market is not None:
                hours_to_expiry[position.market_condition_id] = market.hours_to_expiry

        # Update positions to current prices.
        self.portfolio.update_positions(price_lookup)

        # Evaluate all exit rules: stop-loss, take-profit, edge-closed, expiry.
        decisions = self.portfolio.positions_to_close(
            fair_probabilities=fair_probabilities,
            hours_to_expiry=hours_to_expiry,
        )
        for decision in decisions:
            position = decision.position
            logger.warning(
                f"Closing position ({decision.reason.value}) "
                f"{position.market_condition_id}: {decision.detail}"
            )
            close_trade = self.trader.close_position(position)
            if not close_trade.success:
                logger.error(
                    f"Close failed ({decision.reason.value}) for "
                    f"{position.market_condition_id}: {close_trade.error_message}"
                )
                self.alerts.alert_error(
                    f"Close failed ({decision.reason.value}) for "
                    f"{position.market_condition_id}: {close_trade.error_message}"
                )
                continue

            self.portfolio.trades.append(close_trade)
            self.portfolio.close_position(
                position,
                close_trade.price,
                close_trade.realized_pnl or 0.0,
            )
            self.alerts.alert_position_closed(position, decision.reason.value, close_trade)

        # Log summary
        snapshot = self.portfolio.get_snapshot()
        logger.info(
            f"Portfolio: ${snapshot.total_value:.2f} total | "
            f"{snapshot.open_positions} positions | "
            f"P&L today: ${snapshot.daily_pnl:+.2f}"
        )

    def _shutdown(self):
        """Clean shutdown."""
        logger.info("Shutting down Oracle...")
        if self._telegram_app:
            try:
                self._telegram_app.stop_running()
            except Exception:
                pass
        self.scanner.close()
        self.probability.close()

        snapshot = self.portfolio.get_snapshot()
        self.alerts.alert_daily_summary(snapshot)

        logger.info("Oracle shut down cleanly")


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(description="Polymarket Oracle — AI Trading Bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without executing real trades"
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config file"
    )
    parser.add_argument(
        "--scan-once",
        action="store_true",
        help="Run a single scan cycle and exit"
    )
    args = parser.parse_args()

    # Load config
    config = load_config(args.config)

    # Setup logging
    setup_logging(config)

    if args.scan_once:
        scanner = MarketScanner(config)
        try:
            markets = scanner.scan()
            logger.info(f"Found {len(markets)} tradeable markets")
            for market in markets:
                logger.info(
                    f"[{market.yes_price:.0%}] {market.question} "
                    f"(Vol: ${market.volume_24h:,.0f})"
                )
        finally:
            scanner.close()
        return

    # Create and run oracle
    oracle = Oracle(config, dry_run=args.dry_run)

    # Handle graceful shutdown
    def signal_handler(sig, frame):
        logger.info("Received shutdown signal")
        oracle.running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    oracle.run()


if __name__ == "__main__":
    main()
