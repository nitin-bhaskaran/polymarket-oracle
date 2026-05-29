"""Tests for robust parsing of Claude probability responses."""

from core.models import Market
from core.probability import ProbabilityEngine


def make_market() -> Market:
    return Market(
        condition_id="c1",
        question="Will it happen?",
        slug="will-it",
        yes_token_id="yes",
        no_token_id="no",
        yes_price=0.50,
        no_price=0.50,
    )


def engine() -> ProbabilityEngine:
    return ProbabilityEngine({"anthropic": {"api_key": "test-key"}, "news": {"enabled": False}})


def test_parses_clean_json():
    e = engine()
    text = '{"probability": 0.7, "confidence": 0.8, "reasoning": "x", "key_factors": ["a"]}'
    a = e._parse_response(text, make_market())
    assert a is not None
    assert a.estimated_probability == 0.7
    assert a.confidence == 0.8


def test_parses_json_after_prose():
    """The real failure mode: Claude reasons first, then emits JSON."""
    e = engine()
    text = (
        "I need to assess this carefully.\n\n"
        "Key considerations: base rates, recent form.\n\n"
        '{"probability": 0.35, "confidence": 0.6, "reasoning": "y", "key_factors": ["b","c"]}'
    )
    a = e._parse_response(text, make_market())
    assert a is not None
    assert a.estimated_probability == 0.35


def test_parses_fenced_json():
    e = engine()
    text = '```json\n{"probability": 0.9, "confidence": 0.5, "reasoning": "z", "key_factors": []}\n```'
    a = e._parse_response(text, make_market())
    assert a is not None
    assert a.estimated_probability == 0.9


def test_parses_json_with_trailing_prose():
    e = engine()
    text = '{"probability": 0.42, "confidence": 0.7, "reasoning": "q", "key_factors": ["d"]}\n\nHope that helps!'
    a = e._parse_response(text, make_market())
    assert a is not None
    assert a.estimated_probability == 0.42


def test_clamps_extreme_probability():
    e = engine()
    text = '{"probability": 1.5, "confidence": 0.8, "reasoning": "r", "key_factors": []}'
    a = e._parse_response(text, make_market())
    assert a.estimated_probability == 0.99  # clamped


def test_returns_none_on_no_json():
    e = engine()
    a = e._parse_response("There is no JSON here at all, just prose.", make_market())
    assert a is None


def test_extract_json_object_handles_nested_and_strings():
    e = engine()
    # Braces inside a string value must not confuse the balancer.
    text = 'prefix {"reasoning": "this has a } brace in it", "probability": 0.5, "confidence": 0.5} suffix'
    obj = e._extract_json_object(text)
    assert obj is not None
    import json
    parsed = json.loads(obj)
    assert parsed["probability"] == 0.5
