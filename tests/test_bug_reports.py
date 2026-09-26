"""Bug-report protocol (alloy-0uc.5): parse `<bug>` blocks and capture without routing changes."""

from __future__ import annotations

from conftest import implement_entry, triage_entry
from support import make_bead, make_harness
from test_workflow import script

from alloy.models import BugReport, extract_bug_reports
from alloy.recipes.tdd_loop import (
    context_prompt,
    critic_prompt,
    implement_prompt,
    tests_prompt,
)

NON_BLOCKING_BUG = """<bug>
title: Unrelated legacy typo
where: mypkg/__init__.py:1
evidence: noticed while editing
blocks_task: no
</bug>"""


def _implement_with_trailing_bug() -> dict:
    entry = implement_entry(succeed=True)
    return {**entry, "text": f"{entry['text']}\n\n{NON_BLOCKING_BUG}"}


# -- extract_bug_reports ----------------------------------------------------


def test_extract_bug_reports_parses_two_well_formed_blocks():
    text = """
Done gathering context.

<bug>
title: Broken import in utils
where: src/utils.py:42
evidence: pytest fails with ImportError on import
blocks_task: yes
</bug>

<bug>
title: Typo in README
where: README.md:10
evidence: saw typo while reading docs
blocks_task: no
</bug>
"""
    reports = extract_bug_reports(text)
    assert len(reports) == 2

    assert reports[0] == BugReport(
        title="Broken import in utils",
        where="src/utils.py:42",
        evidence="pytest fails with ImportError on import",
        blocks_task=True,
    )
    assert reports[1] == BugReport(
        title="Typo in README",
        where="README.md:10",
        evidence="saw typo while reading docs",
        blocks_task=False,
    )


def test_extract_bug_reports_returns_empty_when_no_blocks():
    assert extract_bug_reports("no bug markup here") == []


def test_extract_bug_reports_missing_where_defaults_to_empty_string():
    text = """<bug>
title: Something odd
evidence: log line only
blocks_task: no
</bug>"""
    reports = extract_bug_reports(text)
    assert len(reports) == 1
    assert reports[0].title == "Something odd"
    assert reports[0].where == ""


def test_extract_bug_reports_drops_blocks_without_title():
    text = """<bug>
where: orphan.py:1
evidence: no title field
blocks_task: no
</bug>"""
    assert extract_bug_reports(text) == []


def test_extract_bug_reports_collapses_duplicate_titles():
    text = """
<bug>
title: Same issue
where: a.py:1
evidence: first sighting
blocks_task: no
</bug>
<bug>
title: Same issue
where: b.py:2
evidence: second sighting
blocks_task: yes
</bug>
"""
    reports = extract_bug_reports(text)
    assert len(reports) == 1
    assert reports[0].title == "Same issue"


# -- prompts include BUG_PROTOCOL on three roles, not critic ----------------


def test_context_prompt_includes_bug_report_protocol():
    prompt = context_prompt("task brief", "acceptance text")
    assert "<bug>" in prompt
    assert "If the defect blocks your task, stop" in prompt


def test_tests_prompt_includes_bug_report_protocol():
    prompt = tests_prompt("task brief", "acceptance text", {"summary": "ctx"})
    assert "<bug>" in prompt
    assert "If the defect blocks your task, stop" in prompt


def test_implement_prompt_includes_bug_report_protocol():
    prompt = implement_prompt(
        "task brief",
        "acceptance text",
        {"summary": "ctx"},
        "pytest -q",
        "",
        [],
        None,
    )
    assert "<bug>" in prompt
    assert "If the defect blocks your task, stop" in prompt


def test_critic_prompt_excludes_bug_report_protocol():
    prompt = critic_prompt("evidence bundle")
    assert "<bug>" not in prompt
    assert "If the defect blocks your task, stop" not in prompt


# -- workflow: capture only; routing unchanged for non-blocking bugs ----------


async def test_non_blocking_implement_bug_is_recorded_without_changing_outcome(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script())
    baseline_harness = make_harness(project, alloy_home)
    try:
        baseline = await baseline_harness.start()
    finally:
        baseline_harness.close()

    fake_harnesses.reset_calls()
    fake_harnesses.configure(
        script(
            implement=[_implement_with_trailing_bug()],
            triage=[triage_entry("non-blocking")],
        )
    )

    # A fresh bead gets a fresh worktree: the first run's worktree already
    # contains the implementation, so its targeted baseline would be green.
    bug_harness = make_harness(project, alloy_home, bead=make_bead("t-2"))
    try:
        with_bug = await bug_harness.start()
    finally:
        bug_harness.close()

    assert with_bug["outcome"] == baseline["outcome"]
    assert with_bug["iteration"] == baseline["iteration"]

    reported = with_bug.get("reported_bugs", [])
    assert len(reported) == 1
    assert reported[0]["title"] == "Unrelated legacy typo"
    assert reported[0]["reporter"] == "implement"
    assert reported[0]["iteration"] == 1
