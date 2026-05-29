# CLAUDE.md — Polymarket Oracle

## Project: Polymarket Oracle
Autonomous prediction market trading bot for Polymarket.

## Tech Stack
- **Language**: Python 3.11+
- **APIs**: Polymarket Gamma API (data), CLOB V2 API (trading), Anthropic Claude API (probability), Telegram Bot API (alerts)
- **Deployment**: GCP e2-micro VM, systemd
- **Key packages**: py-clob-client-v2, httpx, anthropic, python-telegram-bot, pyyaml, pydantic

## Commands
```
python -m core.main              # Run the bot
python -m core.main --dry-run    # No real trades
python -m core.main --scan-once  # Single scan cycle
python -m core.market_scanner    # List current markets
```

## Architecture
```
core/main.py           → Orchestrator: scan → assess → trade → monitor loop
core/market_scanner.py → Gamma API client, market discovery & filtering
core/probability.py    → Claude API probability assessment engine
core/trader.py         → CLOB API order execution via py-clob-client
core/portfolio.py      → Position tracking, P&L, risk limits, state persistence
core/models.py         → Pydantic data models (Market, Position, Trade, etc.)
alerts/telegram.py     → Telegram notifications + /status /pause /stop commands
```

## Design Decisions
1. Single-threaded scan→assess→trade→sleep cycle (not event-driven)
2. Claude API for probability assessment (~$0.01-0.03 per market)
3. Conservative risk defaults: 10% max per position, 5% minimum edge
4. YAML config for all secrets (gitignored), example file committed
5. Portfolio state persisted to JSON, survives restarts
6. Dry-run mode for safe testing

## Coding Conventions
- Type hints on all functions
- Docstrings on all public methods
- Logging via Python `logging` module (never print)
- All API calls wrapped in try/except with retry logic
- Secrets NEVER in code — config.yaml or environment variables only

## Gotchas
- Polymarket clobTokenIds and outcomePrices are JSON strings inside JSON — need double-parse
- py-clob-client-v2 requires L2 API creds before trading (configure them or derive via L1 auth)
- Gamma API pagination uses offset/limit, max 50 per page
- Market YES + NO prices should sum to ~1.0 but may not exactly
- MarketOrderArgs.amount is USDC to spend, not number of shares
- Telegram bot send_message is async — use sync wrapper in synchronous code
- Portfolio state file at data/portfolio_state.json — back up before redeployments

## Build/Test
```
pip install -r requirements.txt
python -m pytest tests/
python -m core.main --dry-run --scan-once   # Smoke test
```

## Workflow Rules
- Always run --dry-run first after any trade logic changes
- Never commit config.yaml or .env files
- Test market scanner independently before full bot runs
- Check Polymarket API status if getting unexpected 4xx/5xx errors
