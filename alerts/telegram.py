"""
Telegram Alerts — Sends notifications and accepts commands.

Provides real-time notifications for:
- Trade executions
- Edge detection
- Position closures
- Daily P&L summaries
- Errors and warnings

Also accepts commands:
- /status — Current portfolio snapshot
- /positions — List open positions
- /pause — Pause trading
- /resume — Resume trading
- /stop — Emergency stop (cancel all orders)
"""

import logging
from typing import Optional

from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

from core.models import PortfolioSnapshot, ProbabilityAssessment, Trade

logger = logging.getLogger(__name__)


class TelegramAlerts:
    """
    Telegram bot for trade alerts and manual control.
    
    Runs as a background task alongside the main trading loop.
    """
    
    def __init__(self, config: dict):
        tg_config = config.get("telegram", {})
        
        self.bot_token = tg_config.get("bot_token", "")
        self.chat_id = tg_config.get("chat_id", "")
        self.alert_events = tg_config.get("alert_on", [
            "trade_executed", "edge_detected", "position_closed",
            "daily_pnl", "error"
        ])
        
        # Control flags (set by Telegram commands)
        self.trading_paused = False
        
        # Bot instance for sending messages
        self.bot: Optional[Bot] = None
        
        if self.bot_token and self.bot_token != "YOUR_TELEGRAM_BOT_TOKEN_HERE":
            self.bot = Bot(token=self.bot_token)
        else:
            logger.warning("Telegram bot token not configured — alerts disabled")
    
    async def send_message(self, text: str, parse_mode: str = "HTML"):
        """Send a message to the configured chat."""
        if not self.bot or not self.chat_id:
            logger.debug(f"Telegram not configured, would send: {text[:100]}...")
            return
        
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
    
    def send_message_sync(self, text: str):
        """Synchronous wrapper for send_message (for use in sync code)."""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an async context — create a task
                loop.create_task(self.send_message(text))
            else:
                loop.run_until_complete(self.send_message(text))
        except RuntimeError:
            # No event loop — create one
            asyncio.run(self.send_message(text))
    
    # ── Alert Methods ──────────────────────────────────────────────
    
    def alert_trade_executed(self, trade: Trade):
        """Send alert when a trade is executed."""
        if "trade_executed" not in self.alert_events:
            return
        
        status = "✅" if trade.success else "❌"
        dry = " [DRY RUN]" if trade.order_id == "DRY_RUN" else ""
        
        msg = (
            f"{status} <b>Trade Executed{dry}</b>\n\n"
            f"📊 {trade.outcome.value} on market\n"
            f"💰 {trade.size:.1f} shares @ ${trade.price:.3f}\n"
            f"💵 Total: ${trade.total_cost:.2f}\n"
            f"📈 Edge: {trade.edge_at_trade:.1%}\n"
            f"🤖 AI Probability: {trade.ai_probability:.0%}\n"
            f"📉 Market Price: {trade.market_price_at_trade:.0%}"
        )
        
        if not trade.success:
            msg += f"\n\n⚠️ Error: {trade.error_message}"
        
        self.send_message_sync(msg)
    
    def alert_edge_detected(self, assessment: ProbabilityAssessment):
        """Send alert when significant edge is detected."""
        if "edge_detected" not in self.alert_events:
            return
        
        direction = "⬆️ BUY YES" if assessment.edge > 0 else "⬇️ BUY NO"
        
        msg = (
            f"🔍 <b>Edge Detected</b>\n\n"
            f"❓ {assessment.question}\n\n"
            f"{direction}\n"
            f"📈 Edge: {assessment.abs_edge:.1%}\n"
            f"🤖 AI: {assessment.estimated_probability:.0%}\n"
            f"📉 Market: {assessment.market_price:.0%}\n"
            f"🎯 Confidence: {assessment.confidence:.0%}\n\n"
            f"💡 {assessment.reasoning}"
        )
        
        self.send_message_sync(msg)
    
    def alert_daily_summary(self, snapshot: PortfolioSnapshot):
        """Send daily P&L summary."""
        if "daily_pnl" not in self.alert_events:
            return
        
        pnl_emoji = "📈" if snapshot.daily_pnl >= 0 else "📉"
        
        msg = (
            f"📊 <b>Daily Summary</b>\n\n"
            f"{pnl_emoji} Daily P&L: ${snapshot.daily_pnl:.2f}\n"
            f"💰 Total Value: ${snapshot.total_value:.2f}\n"
            f"💵 Available: ${snapshot.available_capital:.2f}\n"
            f"📦 Deployed: ${snapshot.deployed_capital:.2f}\n"
            f"📈 Unrealized: ${snapshot.total_unrealized_pnl:.2f}\n\n"
            f"🔢 Open Positions: {snapshot.open_positions}\n"
            f"📊 Total Trades: {snapshot.total_trades}\n"
            f"✅ Win Rate: {snapshot.win_rate:.0f}%"
        )
        
        self.send_message_sync(msg)
    
    def alert_error(self, error_msg: str):
        """Send alert on error."""
        if "error" not in self.alert_events:
            return
        
        msg = f"🚨 <b>Error</b>\n\n{error_msg}"
        self.send_message_sync(msg)
    
    def alert_startup(self):
        """Send alert when bot starts."""
        msg = (
            "🚀 <b>Polymarket Oracle Started</b>\n\n"
            "Bot is now scanning for opportunities.\n"
            "Commands: /status /positions /pause /resume /stop"
        )
        self.send_message_sync(msg)
    
    def alert_circuit_breaker(self, reason: str):
        """Send alert when circuit breaker triggers."""
        msg = (
            f"⛔ <b>Trading Paused</b>\n\n"
            f"Reason: {reason}\n\n"
            f"Use /resume to restart trading."
        )
        self.send_message_sync(msg)


def setup_command_handlers(
    alerts: TelegramAlerts, 
    get_snapshot_fn, 
    get_positions_fn,
    pause_fn,
    resume_fn,
    emergency_stop_fn,
) -> Optional[Application]:
    """
    Set up Telegram command handlers for manual control.
    
    This creates a long-polling Telegram bot that listens for
    commands like /status, /pause, /resume, /stop.
    
    Returns an Application that needs to be run alongside the main loop.
    """
    if not alerts.bot_token or alerts.bot_token == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        return None
    
    app = Application.builder().token(alerts.bot_token).build()
    
    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        snapshot = get_snapshot_fn()
        pnl_emoji = "📈" if snapshot.daily_pnl >= 0 else "📉"
        await update.message.reply_html(
            f"📊 <b>Portfolio Status</b>\n\n"
            f"💰 Total Value: ${snapshot.total_value:.2f}\n"
            f"💵 Available: ${snapshot.available_capital:.2f}\n"
            f"{pnl_emoji} Daily P&L: ${snapshot.daily_pnl:.2f}\n"
            f"🔢 Open: {snapshot.open_positions} positions\n"
            f"📊 Trades: {snapshot.total_trades} | Win: {snapshot.win_rate:.0f}%\n"
            f"⚡ Trading: {'PAUSED' if alerts.trading_paused else 'ACTIVE'}"
        )
    
    async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
        positions = get_positions_fn()
        if not positions:
            await update.message.reply_text("No open positions.")
            return
        
        msg = "📦 <b>Open Positions</b>\n\n"
        for i, pos in enumerate(positions, 1):
            pnl_emoji = "🟢" if pos.unrealized_pnl >= 0 else "🔴"
            msg += (
                f"{i}. {pos.outcome.value} | "
                f"Entry: ${pos.entry_price:.3f} | "
                f"Now: ${pos.current_price:.3f} | "
                f"{pnl_emoji} {pos.unrealized_pnl_pct:+.1f}%\n"
            )
        await update.message.reply_html(msg)
    
    async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
        alerts.trading_paused = True
        pause_fn()
        await update.message.reply_text("⏸️ Trading paused. Use /resume to continue.")
    
    async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
        alerts.trading_paused = False
        resume_fn()
        await update.message.reply_text("▶️ Trading resumed.")
    
    async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
        alerts.trading_paused = True
        emergency_stop_fn()
        await update.message.reply_text("🛑 Emergency stop! All orders cancelled, trading paused.")
    
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("stop", cmd_stop))
    
    return app
