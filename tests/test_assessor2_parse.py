"""Tests for the hardened two-stage assessor JSON extraction (web-search responses)."""

from core.betfair_assessor2 import TwoStageAssessor


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
