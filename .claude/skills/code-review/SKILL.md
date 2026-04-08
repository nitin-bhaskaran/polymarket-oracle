---
name: code-review
description: Automated code review for Polymarket Oracle. Reviews Python code for security, trading logic, API integration bugs, and risk management gaps. Use when reviewing PRs or code changes.
allowed tools: Read, Grep, Glob, Bash(git diff), Bash(git log)
---

# Code Review Skill — Polymarket Oracle

## Review Focus Areas

### 1. Security (CRITICAL)
- Private keys or API keys never appear in code, logs, or git history
- Config files with secrets are in .gitignore
- No hardcoded credentials, wallet addresses, or tokens
- API key validation before any trading operations
- Telegram bot locked to allowlist after pairing

### 2. Trading Logic
- Edge calculation is correct: `|AI_probability - market_price|`
- BUY YES when AI thinks probability > market, BUY NO when lower
- Position sizing respects max_position_pct of available capital
- MarketOrderArgs.amount is USDC spend, not share count
- Order type (GTC vs FOK) is appropriate for the strategy

### 3. Risk Management
- Stop-loss checks run every cycle
- Daily loss limit enforced
- Consecutive loss circuit breaker active
- Maximum position count respected
- No trading on markets expiring within min_hours_to_expiry
- Price extreme filter (>0.95 or <0.05) prevents bad trades

### 4. API Integration
- Gamma API: JSON strings inside JSON (clobTokenIds, outcomePrices) properly double-parsed
- CLOB API: 2-step initialization (derive creds, then create authenticated client)
- Claude API: Response parsing handles markdown code blocks
- Telegram: async/sync wrapper handles both contexts
- All API calls have timeout and error handling

### 5. State Management
- Portfolio state persists to data/portfolio_state.json
- State loads correctly on restart
- Daily P&L resets at midnight
- Positions reconstructed from saved state

## Review Pattern
Use the AAA pattern:
- **Analyze**: Read the diff and surrounding code
- **Assess**: Score each finding for confidence (0-100)
- **Act**: Report only findings with confidence >= 80

## Output Format
```
## Code Review — [file or feature]

Found N issues:

1. [SEVERITY] Description
   File: path/to/file.py#L10-L15
   Suggestion: ...

2. [SEVERITY] Description
   ...
```

Severity levels: CRITICAL, HIGH, MEDIUM, LOW
