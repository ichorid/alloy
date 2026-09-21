"""alloy.usage.normalize() -- the exact shape and precedence order specified in
"Component 1, item 4" of docs/plans/execution-monitor.md.

This module does not exist yet; these tests fail on import until it does.
"""

from __future__ import annotations

from alloy.usage import normalize

EXPECTED_KEYS = {"input_tokens", "output_tokens", "total_tokens", "cost_usd"}


def test_result_always_has_exactly_the_four_frozen_keys():
    assert set(normalize({})) == EXPECTED_KEYS
    assert set(normalize({"input_tokens": 1, "output_tokens": 2})) == EXPECTED_KEYS


def test_empty_dict_is_all_none():
    assert normalize({}) == {
        "input_tokens": None, "output_tokens": None,
        "total_tokens": None, "cost_usd": None,
    }


def test_snake_case_input_output_sums_to_total():
    assert normalize({"input_tokens": 100, "output_tokens": 20}) == {
        "input_tokens": 100, "output_tokens": 20,
        "total_tokens": 120, "cost_usd": None,
    }


def test_camel_case_input_output_is_recognized():
    assert normalize({"inputTokens": 100, "outputTokens": 20}) == {
        "input_tokens": 100, "output_tokens": 20,
        "total_tokens": 120, "cost_usd": None,
    }


def test_openai_style_prompt_completion_is_recognized():
    assert normalize({"prompt_tokens": 50, "completion_tokens": 10}) == {
        "input_tokens": 50, "output_tokens": 10,
        "total_tokens": 60, "cost_usd": None,
    }


def test_totals_only_shape_with_no_output_key_never_guesses_a_split():
    """The fake Pi harness emits exactly this: {"input_tokens": 100} with nothing
    to distinguish it from a real (albeit output-less) input count, so the plan
    requires treating it as an ambiguous total, not a real input value."""
    assert normalize({"input_tokens": 100}) == {
        "input_tokens": None, "output_tokens": None,
        "total_tokens": 100, "cost_usd": None,
    }


def test_snake_case_takes_precedence_over_camel_case_when_both_present():
    raw = {"input_tokens": 1, "output_tokens": 2, "inputTokens": 999, "outputTokens": 999}
    result = normalize(raw)
    assert result["input_tokens"] == 1
    assert result["output_tokens"] == 2
    assert result["total_tokens"] == 3


def test_cost_usd_is_carried_through_verbatim():
    result = normalize({"input_tokens": 1, "output_tokens": 2, "total_cost_usd": 0.0123})
    assert result["cost_usd"] == 0.0123


def test_cost_usd_is_none_when_not_reported():
    result = normalize({"input_tokens": 1, "output_tokens": 2})
    assert result["cost_usd"] is None


def test_unknown_fields_are_ignored_for_token_totals():
    raw = {
        "input_tokens": 5, "output_tokens": 1,
        "num_turns": 3, "session_id": "abc", "total_cost_usd": None,
    }
    assert normalize(raw) == {
        "input_tokens": 5, "output_tokens": 1,
        "total_tokens": 6, "cost_usd": None,
    }
