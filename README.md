# Polymarket Oracle 🔮

Autonomous prediction market trading bot for [Polymarket](https://polymarket.com). Uses AI-driven probability assessment to identify mispriced markets and execute trades automatically..

> **Running the Betfair paper loop?** See [`RUNBOOK.md`](./RUNBOOK.md) for the
> start / stop / check-results steps (and the pre-flight checklist: config sync,
> VPN-off, sleep-disabled).

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 Polymarket Oracle                │
├─────────────┬───────────────┬───────────────────┤
│  Market      │  Strategy     │  Execution        │
│  Scanner     │  Engine       │  Engine           │
│             │               │                   │
│ • Gamma API  │ • News fetch  │ • CLOB API        │
│ • Price mon  │ • Claude API  │ • Order mgmt      │
│ • Volume     │ • Probability │ • Position track   │
│ • Filters    │ • Edge calc   │ • Risk limits      │
├─────────────┴───────────────┴───────────────────┤
│                 Telegram Alerts                   │
│  • Trade notifications  • P&L updates            │
│  • Edge alerts          • Manual override cmds    │
└─────────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites
- Python 3.11+
- Polymarket account with USDC on Polygon
- Gemini and/or Anthropic API key (for probability assessment)
- Telegram bot token (for alerts)

### Setup

```bash
# Clone
git clone https://github.com/nitin-bhaskaran/polymarket-oracle.git
cd polymarket-oracle

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Configure
cp config/config.example.yaml config/config.yaml
# Edit config.yaml with your credentials

# Run
python -m core.main
```

For a credentials-light smoke test, `python -m core.main --scan-once` only
exercises public market discovery and does not initialize the LLM or trading
clients.

### GCP Deployment

```bash
# SSH into your GCP VM
gcloud compute ssh polymarket-oracle-vm

# Clone and setup
git clone https://github.com/nitin-bhaskaran/polymarket-oracle.git
cd polymarket-oracle
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp config/config.example.yaml config/config.yaml
nano config/config.yaml  # Add your credentials

# Run with systemd (survives reboots)
sudo cp scripts/polymarket-oracle.service /etc/systemd/system/
sudo systemctl enable polymarket-oracle
sudo systemctl start polymarket-oracle

# Check logs
sudo journalctl -u polymarket-oracle -f
```

## Configuration

All configuration is in `config/config.yaml`. See `config/config.example.yaml` for all options.

Key settings:
- `polymarket.private_key`: Your Polygon wallet private key
- `polymarket.funder_address`: Your Polymarket proxy wallet address
- `polymarket.clob_api_*`: Optional pre-created CLOB V2 API credentials
- `anthropic.api_key`: For AI probability assessment
- `gemini.api_key`: Optional cheap-first assessment key (or set `GEMINI_API_KEY`)
- `news.enabled`: Whether to enrich assessments with public news context
- `telegram.bot_token`: For trade alerts
- `telegram.chat_id`: Your Telegram user ID
- `risk.max_position_pct`: Max % of capital per trade (default: 10%)
- `risk.min_edge`: Minimum edge to trade (default: 5%)

## Strategy

The bot runs a **news-driven probability repricing** strategy:

1. **Scan** — Fetches all active markets from Polymarket Gamma API
2. **Filter** — Selects markets with sufficient liquidity and volume
3. **Assess** — For each candidate, fetches recent public news and uses Claude to estimate true probability (concurrent, with rate-limit-aware retry)
4. **Compare** — Calculates edge: `|AI_probability - market_price|`
5. **Size** — Confidence- and spread-aware fractional Kelly (see below)
6. **Trade** — If edge exceeds threshold and survives the spread, places a trade via CLOB API
7. **Monitor** — Tracks positions, evaluates all exit rules, reconciles fills
8. **Alert** — Sends Telegram notifications for all actions

### Position sizing

Rather than a flat percentage per trade, the bot sizes each position by signal
quality using fractional Kelly:

```
f* = (fair_prob - price) / (1 - price)      # full Kelly for a binary token
size_fraction = f* × kelly_fraction × confidence
```

The result is clamped to `max_position_pct` and to available capital, and the
half-spread is subtracted from the edge first — if the edge doesn't survive the
spread, the trade is skipped. Set `use_kelly_sizing: false` to fall back to a
flat confidence-scaled size.

### Exit rules

A position is closed on the first of: **near-expiry** (within
`exit_hours_before_expiry`), **stop-loss**, **take-profit**, or **edge-closed**
(the market price has caught up to the AI's fair value, so the thesis is gone).
Each rule is independently toggleable in config.

## Risk Management

- Confidence/spread-aware position sizing, capped at `max_position_pct` (default 10%)
- Maximum number of concurrent positions (default: 10)
- Stop-loss and take-profit per position (configurable)
- Edge-closed and near-expiry exits
- Daily loss limit
- Circuit breaker on consecutive losses
- No trading on markets expiring too soon

## Backtesting

Before risking capital, test the edge/sizing premise on resolved markets:

```bash
python -m core.backtest --data data/backtest_sample.json --capital 130 --min-edge 0.05
```

The dataset (JSON or CSV) needs, per market: `yes_price`, `ai_probability`,
`confidence`, `outcome` (1=YES, 0=NO), and optionally `spread`. The harness
replays each row through the *same* sizing logic the live bot uses and reports
win rate, ROI, and the AI's Brier calibration score. It makes no live API calls.

## Health monitoring

Each cycle writes `data/health.json` with a timestamp, cycle count, capital, and
open-position count. Check its modification time to confirm the bot is alive.

## Current Build Stage

This repo is primarily in paper-trading hardening. The Betfair path now has a
separately gated small-bankroll live executor, but its end-to-end order path
cannot be validated with the free delayed application key.

- Betfair live application access has been activated and a minimum-stake order
  has been verified
- Probability assessments have enough fresh news/context for the target market categories
- The test suite passes locally and on CI

### Betfair validation sleeves

The Betfair paper path keeps broad market discovery, then routes matching
markets into configurable strategy sleeves. Sleeves can restrict their own
market types and enforce independent exposure limits without narrowing the
rest of the scanner.

The default configuration includes:

- `fifa_world_cup`: liquid, modelable FIFA World Cup markets (`MATCH_ODDS`,
  `OVER_UNDER_25`, `BOTH_TEAMS_TO_SCORE`, and `DRAW_NO_BET`), capped at 25%
  total bankroll exposure, 3% per market, and three open markets per match.
- `general`: all other markets, with its own sleeve, event, and market limits.
- A paper-only portfolio-wide 75% open-liability ceiling and 50-open-bet
  ceiling. These limits preserve bankroll realism without starving the
  validation sample.
- Up to five entries per market and three per selection. Different selections
  are allowed, while a repeated entry on the same selection requires a fresh
  material improvement in odds or assessed edge. Same-selection entries scale
  down to 100%, 50%, then 25% of the fresh Kelly recommendation.

Existing paper records remain tagged `legacy`; new records include domain,
competition, event, market type, sleeve, and strategy attribution. The paper
analysis compares AI Brier score directly with the Betfair market baseline.

### Assessment cost routing

The Betfair assessor tries providers in `betfair_assessor.provider_order`.
The default is Gemini first and Anthropic second. With `GEMINI_API_KEY` set,
Gemini Flash-Lite handles no-search triage and Gemini Flash handles grounded
deep assessments. Anthropic Haiku/Sonnet is used automatically if Gemini is
unconfigured, rate-limited, or returns unusable output.

`paper.daily_paid_deep_assessment_budget` separately caps Sonnet fallbacks
(default: 5/day), so a free-tier outage cannot silently produce an open-ended
Anthropic bill. Remove `anthropic` from `provider_order` for strict mode with
no paid fallback.

Every paper bet stores `assessment_provider` and `assessment_model`; run
`python -m core.paper_analysis` to compare calibration and ROI by provider
before deciding whether the cheaper route is good enough for live capital.

### Betfair live execution

Real-money Betfair execution is available only through the explicit `--live`
mode. Paper mode remains the default. Live mode refuses to start unless:

- `live.enabled: true`
- `betfair.app_key_mode: live`
- `BETFAIR_LIVE_ACK=I_ACCEPT_REAL_MONEY_RISK`
- Betfair's `getDeveloperAppKeys` confirms the configured key is active and
  non-delayed
- account balance and existing exposure pass the configured limits

The default £10 experiment allows one order per cycle, two per UTC day, £2
maximum liability per bet, £3 aggregate exposure, and a £5 cumulative loss
stop. It initially trades only the paper run's candidate slice: LAY signals,
8-12% assessed edge, and 50-75% confidence. All orders are synchronous
`FILL_OR_KILL` LIMIT orders, so unmatched orders never remain in the market.
Every attempt is appended to `data/live_order_attempts.jsonl`; accepted orders
are stored separately in `data/live_bets.jsonl`.

Betfair currently distinguishes free delayed development access from activated
live betting access. The developer site lists a £499 activation fee for live
Exchange API use. Depositing £10 does not remove that API requirement.

## License

MIT
