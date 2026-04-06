"""
Polymarket Oracle — Main entry point and orchestrator.

Runs the continuous scan → assess → trade → monitor loop.
Coordinates the market scanner, probability engine, trader,
portfolio manager, and Telegram alerts.
"""

import argparse
import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

import yaml

from core.market_scanner import MarketScanner
from core.probability import ProbabilityEngine
from core.trader import Trader
from core.portfolio import PortfolioManager
from alerts.telegram import TelegramAlerts, setup_command_handlers

logger = logging.getLogger("oracle")


def setup_logging(config: dict):
    """Configure logging based on config settings."""
    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO").upper())
    log_file = log_config.get("file", "logs/oracle.log")
    
    # Create log directory
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    
    # Configure root logger
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ]
    )


def load_config(config_path: str = "config/config.yaml") -> dict:
    """Load configuration from YAML file."""
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
        logger.info(f"Configuration loaded from {config_path}")
        return config
    except FileNotFoundError:
        logger.error(
            f"Config file not found: {config_path}\n"
            f"Copy config/config.example.yaml to config/config.yaml "
            f"and fill in your credentials."
        )
        sys.exit(1)


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
        
        self.scanner = MarketScanner(config)
        self.probability = ProbabilityEngine(config)
        self.trader = Trader(config)
        self.portfolio = PortfolioManager(config)
        self.alerts = TelegramAlerts(config)
        
        if dry_run:
            self.trader.dry_run = True
            logger.info("🔸 Running in DRY RUN mode — no real trades will be executed")
        
        # Initialize trader (derives API keys)
        self.trader.initialize()
        
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
                    
                    # Execute
                    trade = self.trader.execute_trade(
                        market=market,
                        assessment=assessment,
                        available_capital=self.portfolio.available_capital,
                    )
                    
                    if trade:
                        position = self.portfolio.record_trade(trade)
                        self.alerts.alert_trade_executed(trade)
                
                # ── STEP 4: MONITOR ──
                logger.info("Step 4: Monitoring positions...")
                self._monitor_positions()
                
                # ── STEP 5: REPORT ──
                # Send daily summary at end of each cycle
                if cycle_count % 12 == 0:  # Every ~1 hour (12 x 5 min)
                    snapshot = self.portfolio.get_snapshot()
                    self.alerts.alert_daily_summary(snapshot)
                
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
    
    def _monitor_positions(self):
        """Update positions and check stop-losses."""
        if not self.portfolio.open_positions:
            return
        
        # Build price lookup from scanner
        price_lookup = {}
        for position in self.portfolio.open_positions:
            price = self.scanner.get_current_price(position.token_id)
            if price is not None:
                price_lookup[position.token_id] = price
        
        # Update positions
        self.portfolio.update_positions(price_lookup)
        
        # Check stop-losses
        stop_loss_positions = self.portfolio.check_stop_losses()
        for position in stop_loss_positions:
            logger.warning(f"Closing position due to stop-loss: {position.market_condition_id}")
            # In production, you'd sell the position here
            # For now, just mark it as closed
            self.portfolio.close_position(
                position, 
                position.current_price,
                position.unrealized_pnl,
            )
        
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
    
    # Create and run oracle
    oracle = Oracle(config, dry_run=args.dry_run)
    
    # Handle graceful shutdown
    def signal_handler(sig, frame):
        logger.info("Received shutdown signal")
        oracle.running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    if args.scan_once:
        # Single cycle mode
        oracle.running = True
        markets = oracle.scanner.scan()
        print(f"\nFound {len(markets)} tradeable markets:")
        for m in markets:
            print(f"  [{m.yes_price:.0%}] {m.question} (Vol: ${m.volume_24h:,.0f})")
        oracle._shutdown()
    else:
        oracle.run()


if __name__ == "__main__":
    main()
