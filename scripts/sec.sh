#!/bin/bash
# Pre-commit hook: blocks commits containing secrets
# Checks for private keys, API tokens, and sensitive patterns

echo "🔒 Running security check..."

# Patterns that should NEVER appear in committed code
PATTERNS=(
    "sk-ant-"           # Anthropic API keys
    "PRIVATE_KEY.*=.*[a-fA-F0-9]{64}"  # Ethereum private keys
    "ghp_"              # GitHub personal access tokens
    "bot_token.*=.*[0-9]+:AA"  # Telegram bot tokens
    "password.*=.*['\"]"  # Hardcoded passwords
)

FOUND=0
for pattern in "${PATTERNS[@]}"; do
    if git diff --cached --diff-filter=ACM | grep -qiE "$pattern"; then
        echo "❌ BLOCKED: Found potential secret matching pattern: $pattern"
        FOUND=1
    fi
done

if [ $FOUND -eq 1 ]; then
    echo ""
    echo "⛔ Commit blocked. Remove secrets before committing."
    echo "   Secrets belong in config/config.yaml (gitignored) or environment variables."
    exit 1
fi

echo "✅ Security check passed"
exit 0
