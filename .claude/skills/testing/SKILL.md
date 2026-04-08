---
name: testing
description: Testing patterns for Polymarket Oracle. Generates and runs tests for market scanning, probability assessment, trading, and portfolio management. Use when writing or running tests.
allowed tools: Read, Grep, Glob, Bash
---

# Testing Skill — Polymarket Oracle

## Test Strategy

### Unit Tests
- Use pytest with fixtures
- Mock all external API calls (Polymarket, Claude, Telegram)
- Use factory pattern for test data (Market, Position, Trade objects)
- Test each module independently

### Key Test Scenarios

#### Market Scanner (`tests/test_scanner.py`)
- Parses valid Gamma API response correctly
- Handles missing/null fields gracefully
- Filters by liquidity, volume, expiry, price extremes
- Pagination works across multiple pages
- Empty response returns empty list (no crash)

#### Probability Engine (`tests/test_probability.py`)
- Parses valid Claude JSON response
- Handles markdown code blocks in response
- Handles malformed JSON gracefully
- Clamps probability to 0.01-0.99 range
- Edge calculation is correct (signed and absolute)
- Recommended side is correct based on edge direction

#### Trader (`tests/test_trader.py`)
- Dry run mode logs but doesn't execute
- Position size calculation respects max_position_pct
- Minimum edge threshold enforced
- Correct token_id selected based on edge direction
- Handles CLOB API errors without crashing

#### Portfolio (`tests/test_portfolio.py`)
- Can trade check: position limits, daily loss, consecutive losses, capital
- Trade recording updates available capital correctly
- Stop-loss detection at threshold
- State persistence: save and reload produces same state
- Daily reset clears P&L counter

### Test Data Factories
```python
def make_market(**overrides) -> Market:
    defaults = {
        "condition_id": "test_condition_123",
        "question": "Will X happen by Y?",
        "slug": "test-market",
        "yes_token_id": "token_yes_123",
        "no_token_id": "token_no_123",
        "yes_price": 0.65,
        "no_price": 0.35,
        "liquidity": 10000.0,
        "volume_24h": 5000.0,
    }
    defaults.update(overrides)
    return Market(**defaults)
```

### Running Tests
```bash
python -m pytest tests/ -v
python -m pytest tests/test_scanner.py -v
python -m pytest tests/ -k "test_edge" -v
python -m pytest tests/ --cov=core --cov-report=term-missing
```
