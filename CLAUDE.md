# CLAUDE.md — Polymarket Oracle

## Project Overview
Autonomous prediction market trading bot for Polymarket. Uses AI (Claude API) to assess event probabilities, compares against market prices, and executes trades when edge is detected.

## Owner
Nitin Bhaskaran (nitin-bhaskaran on GitHub). Based in London. Experienced in post-trade technology, familiar with Python syntax but hasn't written production code recently — keep code clear and well-commented.

## Tech Stack
- **Language**: Python 3.11+
- **APIs**: Polymarket Gamma API (market data), Polymarket CLOB API (trading), Anthropic Claude API (probability assessment), Telegram Bot API (alerts)
- **Deployment**: GCP e2-micro VM, systemd service
- **Key packages**: py-clob-client, httpx, anthropic, python-telegram-bot, pyyaml, schedule

## Project Structure
```
polymarket-oracle/
├── core/
│   ├── main.py           # Entry point, orchestrator loop
│   ├── market_scanner.py # Gamma API client, market discovery
│   ├── probability.py    # Claude API probability assessment
│   ├── trader.py         # CLOB API order execution
│   ├── portfolio.py      # Position tracking, P&L
│   └── models.py         # Data models (Market, Position, Trade)
├── strategies/
│   ├── news_edge.py      # News-driven probability repricing
│   └── base.py           # Strategy base class
├── alerts/
│   └── telegram.py       # Telegram bot for notifications
├── config/
│   ├── config.example.yaml
│   └── config.yaml       # (gitignored) actual config
├── scripts/
│   ├── setup_gcp.sh      # GCP VM setup script
│   └── polymarket-oracle.service  # systemd unit file
├── tests/
│   └── ...
├── requirements.txt
├── .gitignore
├── .env.example
└── README.md
```

## Key Design Decisions
1. **Single-threaded async loop** — scan → assess → trade → sleep cycle, not event-driven
2. **Claude API for probability** — Each market assessment costs ~$0.01-0.03 in API calls
3. **Conservative risk defaults** — 10% max per position, 5% minimum edge
4. **Telegram for alerts only** — Bot sends notifications; manual overrides via Telegram commands
5. **YAML config** — All secrets in config.yaml (gitignored), example file committed

## Coding Conventions
- Type hints on all functions
- Docstrings on all public methods
- Logging via Python `logging` module (not print statements)
- All API calls wrapped in try/except with retry logic
- Secrets NEVER in code — always from config.yaml or environment variables

## Safety Rules
- **Tier 1 (autonomous)**: Market scanning, price checks, probability assessment, Telegram alerts
- **Tier 2 (log + execute)**: Order placement, position management — always log before executing
- **Tier 3 (never auto)**: Changing risk parameters, withdrawing funds, modifying this file

## Common Tasks
- `python -m core.main` — Run the bot
- `python -m core.main --dry-run` — Run without executing trades
- `python -m tests.test_scanner` — Test market scanner
- `python -m core.market_scanner --list` — List current markets
