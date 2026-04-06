# Polymarket Oracle 🔮

Autonomous prediction market trading bot for [Polymarket](https://polymarket.com). Uses AI-driven probability assessment to identify mispriced markets and execute trades automatically.

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
- Anthropic API key (for probability assessment)
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
- `anthropic.api_key`: For AI probability assessment
- `telegram.bot_token`: For trade alerts
- `telegram.chat_id`: Your Telegram user ID
- `risk.max_position_pct`: Max % of capital per trade (default: 10%)
- `risk.min_edge`: Minimum edge to trade (default: 5%)

## Strategy

The bot runs a **news-driven probability repricing** strategy:

1. **Scan** — Fetches all active markets from Polymarket Gamma API
2. **Filter** — Selects markets with sufficient liquidity and volume
3. **Assess** — For each candidate, fetches recent news and uses Claude to estimate true probability
4. **Compare** — Calculates edge: `|AI_probability - market_price|`
5. **Trade** — If edge exceeds threshold, places a trade via CLOB API
6. **Monitor** — Tracks positions, P&L, and market movements
7. **Alert** — Sends Telegram notifications for all actions

## Risk Management

- Maximum position size as % of capital (default: 10%)
- Maximum number of concurrent positions (default: 10)
- Stop-loss per position (configurable)
- Daily loss limit
- Circuit breaker on consecutive losses
- No trading on markets expiring within 1 hour

## License

MIT
