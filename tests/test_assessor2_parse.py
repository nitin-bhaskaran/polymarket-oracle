"""Tests for the hardened two-stage assessor JSON extraction (web-search responses)."""

from core.betfair_assessor2 import TwoStageAssessor
from core.betfair_models import BetfairMarket, PriceLevel, Runner


def ts():
    return TwoStageAssessor({"anthropic": {"api_key": "test"}, "betfair_assessor": {}})


def test_clean_json():
    a = ts()
    d = a._extract_json('{"probabilities": {"1": 0.6, "2": 0.4}, "confidence": 0.8}')
    assert d["probabilities"]["1"] == 0.6


def test_json_after_search_narration_with_braces():
    """The real Arnaldi failure mode: narration containing braces before the answer."""
    a = ts()
    messy = ('Let me search {recent form} and {injuries}.\n'
             'Assessment:\n'
             '{"probabilities": {"1": 0.42, "2": 0.58}, "confidence": 0.7, '
             '"reasoning": "key player {out}"}')
    d = a._extract_json(messy)
    assert d is not None
    assert abs(sum(d["probabilities"].values()) - 1.0) < 1e-9


def test_prefers_object_with_probabilities():
    a = ts()
    multi = '{"plan": "search"} {"probabilities": {"1": 0.7, "2": 0.3}, "confidence": 0.9}'
    d = a._extract_json(multi)
    assert "probabilities" in d
    assert d["probabilities"]["1"] == 0.7


def test_last_probabilities_object_wins():
    a = ts()
    # A draft then a final answer — take the final.
    txt = ('{"probabilities": {"1": 0.5, "2": 0.5}, "confidence": 0.4}\n'
           'On reflection:\n'
           '{"probabilities": {"1": 0.65, "2": 0.35}, "confidence": 0.8}')
    d = a._extract_json(txt)
    assert d["probabilities"]["1"] == 0.65


def test_no_json_returns_none():
    a = ts()
    assert a._extract_json("I couldn't find enough information.") is None


def test_fenced_json():
    a = ts()
    d = a._extract_json('```json\n{"probabilities": {"1": 1.0}, "confidence": 0.5}\n```')
    assert d["probabilities"]["1"] == 1.0


def test_nested_braces_in_strings_dont_break():
    a = ts()
    d = a._extract_json('{"probabilities": {"1": 0.5, "2": 0.5}, "reasoning": "a } in text"}')
    assert d is not None
    assert len(d["probabilities"]) == 2


def provider_ts():
    return TwoStageAssessor({
        "anthropic": {"api_key": "anthropic-test"},
        "gemini": {
            "api_key": "gemini-test",
            "triage_model": "gemini-cheap",
            "deep_model": "gemini-grounded",
        },
        "betfair_assessor": {
            "provider_order": ["gemini", "anthropic"],
            "triage_model": "claude-cheap",
            "deep_model": "claude-grounded",
        },
    })


def test_configured_providers_follow_routing_order():
    assert provider_ts().configured_providers() == ["gemini", "anthropic"]
    assert ts().configured_providers() == ["anthropic"]


def market():
    return BetfairMarket(
        market_id="1.2",
        event_name="A v B",
        market_name="Match Odds",
        runners=[
            Runner(
                selection_id=1,
                name="A",
                available_to_back=[PriceLevel(price=2.0, size=100)],
            ),
            Runner(
                selection_id=2,
                name="B",
                available_to_back=[PriceLevel(price=2.0, size=100)],
            ),
        ],
    )


def test_triage_prefers_gemini_and_attributes_result(monkeypatch):
    assessor = provider_ts()
    monkeypatch.setattr(
        assessor,
        "_gemini_call",
        lambda **kwargs: (
            '{"probabilities": {"1": 0.6, "2": 0.4}, "confidence": 0.7}',
            0,
            {},
        ),
    )
    monkeypatch.setattr(
        assessor,
        "_anthropic_call",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Anthropic should not be called")
        ),
    )

    _, assessments = assessor.triage(market())

    assert assessments[0].assessment_provider == "gemini"
    assert assessments[0].assessment_model == "gemini-cheap"


def test_triage_falls_back_to_anthropic(monkeypatch):
    assessor = provider_ts()
    monkeypatch.setattr(
        assessor,
        "_gemini_call",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("quota exhausted")),
    )
    monkeypatch.setattr(
        assessor,
        "_anthropic_call",
        lambda **kwargs: (
            '{"probabilities": {"1": 0.55, "2": 0.45}, "confidence": 0.6}',
            0,
            object(),
        ),
    )

    _, assessments = assessor.triage(market())

    assert assessments[0].assessment_provider == "anthropic"
    assert assessments[0].assessment_model == "claude-cheap"


def test_gemini_deep_call_enables_search_grounding(monkeypatch):
    assessor = provider_ts()
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "candidates": [{
                    "content": {"parts": [{"text": '{"probabilities": {}}'}]},
                    "groundingMetadata": {
                        "webSearchQueries": ["team news", "injuries"],
                    },
                }],
            }

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("core.betfair_assessor2.httpx.post", fake_post)
    _, searches, _ = assessor._gemini_call(
        model="gemini-grounded",
        system="system",
        prompt="prompt",
        max_tokens=100,
        grounded=True,
    )

    assert captured["json"]["tools"] == [{"google_search": {}}]
    assert "responseMimeType" not in captured["json"]["generationConfig"]
    assert searches == 2


def test_deep_skips_anthropic_when_paid_fallback_is_disabled(monkeypatch):
    assessor = provider_ts()
    assessor.allow_paid_deep_fallback = False
    monkeypatch.setattr(
        assessor,
        "_gemini_call",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("quota exhausted")),
    )
    monkeypatch.setattr(
        assessor,
        "_anthropic_call",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("paid fallback should be blocked")
        ),
    )

    assert assessor.deep_assess(market()) == []
    assert assessor.paid_deep_used is False
