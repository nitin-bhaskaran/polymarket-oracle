from core.models import Market
from core.probability import ProbabilityEngine


class FakeNewsResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "articles": [
                {
                    "title": "Relevant update moves the market",
                    "domain": "example.com",
                    "seendate": "20260529T120000Z",
                    "url": "https://example.com/update",
                }
            ]
        }


class FakeNewsClient:
    def __init__(self):
        self.params = None

    def get(self, url, params):
        self.params = params
        return FakeNewsResponse()


def make_market(**overrides) -> Market:
    defaults = {
        "condition_id": "condition-1",
        "question": "Will the test pass by Friday?",
        "slug": "will-the-test-pass",
        "yes_token_id": "yes-token",
        "no_token_id": "no-token",
        "description": "Resolves yes if the test passes.",
        "event_title": "Test Event",
    }
    defaults.update(overrides)
    return Market(**defaults)


def test_fetch_news_context_includes_gdelt_articles():
    engine = ProbabilityEngine(
        {
            "anthropic": {"api_key": "test-key"},
            "news": {"enabled": True, "provider": "gdelt", "max_articles": 1},
        }
    )
    fake_client = FakeNewsClient()
    engine.news_client = fake_client

    context = engine._fetch_news_context(make_market())

    assert "Recent news:" in context
    assert "Relevant update moves the market" in context
    assert "https://example.com/update" in context
    assert fake_client.params["mode"] == "artlist"
    assert "sourcelang:english" in fake_client.params["query"]


def test_fetch_news_context_falls_back_when_disabled():
    engine = ProbabilityEngine(
        {"anthropic": {"api_key": "test-key"}, "news": {"enabled": False}}
    )

    context = engine._fetch_news_context(make_market())

    assert "Resolution criteria: Resolves yes if the test passes." in context
    assert "Recent news:" not in context
