"""Journal 39: extract implementer summary from a marked <summary> block."""

from __future__ import annotations

from alloy.models import clip
from alloy.recipes.tdd_loop import extract_summary, implement_prompt


def test_extract_summary_returns_last_block_stripped():
    text = (
        "Some preamble about background work.\n\n"
        "<summary>Changed X.</summary>\n\n"
        "Trailing chatter that must not appear in change_summary."
    )
    assert extract_summary(text) == "Changed X."


def test_extract_summary_returns_last_when_multiple_blocks():
    text = "<summary>First summary.</summary>\n\n<summary>Second summary.</summary>"
    assert extract_summary(text) == "Second summary."


def test_extract_summary_strips_whitespace_inside_block():
    text = "<summary>  Implemented slugify.  </summary>"
    assert extract_summary(text) == "Implemented slugify."


def test_extract_summary_preserves_multiline_block_content():
    text = "<summary>Changed slugify.\nAdded edge-case test.</summary>"
    assert extract_summary(text) == "Changed slugify.\nAdded edge-case test."


def test_extract_summary_falls_back_to_clip_when_no_tags():
    text = "no summary tags here"
    assert extract_summary(text) == clip(text, 600)


def test_extract_summary_falls_back_to_clip_for_long_untagged_text():
    text = "X" * 5000
    assert extract_summary(text) == clip(text, 600)


def test_implement_prompt_requires_summary_marker_with_tests_edited_line():
    prompt = implement_prompt(
        "task brief",
        "acceptance text",
        {"summary": "ctx"},
        "pytest -q",
        "",
        [],
        None,
    )
    assert "<summary>" in prompt
    assert "</summary>" in prompt
    assert "tests edited:" in prompt
