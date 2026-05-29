from datetime import datetime, timedelta, timezone

from core.market_scanner import MarketScanner


def test_parse_market_handles_gamma_json_strings_and_aware_expiry():
    scanner = MarketScanner({})
    end_date = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat().replace(
        "+00:00", "Z"
    )

    market = scanner.parse_market(
        {
            "conditionId": "condition-1",
            "question": "Will the test pass?",
            "slug": "will-the-test-pass",
            "id": "market-1",
            "clobTokenIds": '["yes-token", "no-token"]',
            "outcomePrices": '["0.61", "0.39"]',
            "description": "Resolution criteria",
            "endDate": end_date,
            "active": True,
            "closed": False,
            "liquidityNum": "12345.67",
            "volume24hr": "8901.23",
            "volumeNum": "45678.90",
        },
        event_context={"title": "Test Event", "slug": "test-event", "category": "testing"},
    )

    assert market is not None
    assert market.yes_token_id == "yes-token"
    assert market.no_token_id == "no-token"
    assert market.yes_price == 0.61
    assert market.no_price == 0.39
    assert market.category == "testing"
    assert market.hours_to_expiry > 3.5


def test_filters_reject_price_extremes_and_low_liquidity():
    scanner = MarketScanner({"risk": {"min_liquidity": 100, "min_volume_24h": 50}})
    base = {
        "conditionId": "condition-1",
        "question": "Will the test pass?",
        "slug": "will-the-test-pass",
        "clobTokenIds": '["yes-token", "no-token"]',
        "outcomePrices": '["0.50", "0.50"]',
        "liquidity": "500",
        "volume24hr": "250",
    }

    valid = scanner.parse_market(base)
    extreme = scanner.parse_market({**base, "conditionId": "condition-2", "outcomePrices": '["0.99", "0.01"]'})
    low_liquidity = scanner.parse_market({**base, "conditionId": "condition-3", "liquidity": "1"})

    filtered = scanner._apply_filters([valid, extreme, low_liquidity])

    assert filtered == [valid]
