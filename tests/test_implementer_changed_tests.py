"""alloy-5wb.3: tell acceptance gate and judge which tests the implementer changed.

Tests encode journal 23 acceptance criteria. Expected to fail until prove_red
fingerprints and post-implement diffing land.
"""

from __future__ import annotations

import hashlib
import inspect
import sys

from alloy.models import Attempt
from alloy.recipes.tdd_loop import judge_prompt
from alloy.worktree import WorktreeManager
from conftest import (
    acceptance_entry,
    context_entry,
    implement_add_test_entry,
    implement_entry,
    implement_rewrite_helper_test_entry,
    implement_rewrite_tests_entry,
    judge_entry,
    synthesize_entry,
    verifier_run_entry,
    verifier_stop_entry,
    write_tests_entry,
    write_tests_with_helper_entry,
)
from support import make_harness

FULL_SUITE = f"{sys.executable} -m pytest -q"
IMPLEMENTER_TESTS_HEADING = "## Tests changed by the implementer"
SLUGIFY_TEST = "tests/test_slugify.py"
HELPER_TEST = "tests/test_helper.py"
EXTRA_TEST = "tests/test_extra.py"


def _implementer_tests_section(prompt: str) -> str:
    start = prompt.index(IMPLEMENTER_TESTS_HEADING) + len(IMPLEMENTER_TESTS_HEADING)
    rest = prompt[start:].lstrip("\n")
    end = rest.find("\n## ")
    return rest[:end].strip() if end != -1 else rest.strip()


def _acceptance_script(**overrides):
    base = {
        "context": context_entry(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "verifier": [
            verifier_run_entry(FULL_SUITE, kind="regression"),
            verifier_stop_entry("regression suite green"),
        ],
        "acceptance": [acceptance_entry("accept", confidence=0.9)],
        "judge": [],
        "critic": {"structured": {"root_cause": "n/a", "evidence": "n/a", "suggested_fix": "n/a", "confidence": 0.5}},
        "synthesize": synthesize_entry(),
    }
    base.update(overrides)
    return base


def _escalate_to_judge_script(**overrides):
    return _acceptance_script(
        acceptance=[acceptance_entry("escalate", reason="needs stronger judge")],
        judge=[judge_entry("done")],
        **overrides,
    )


# ---------------------------------------------------------------------------
# WorktreeManager.fingerprints
# ---------------------------------------------------------------------------


def test_worktree_manager_fingerprints_sha256_of_file_contents(project, tmp_path):
    manager = WorktreeManager(repo=project, root=tmp_path / "worktrees")
    worktree = manager.ensure("bd-1")
    target = worktree.path / SLUGIFY_TEST
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "def test_marker():\n    assert True\n"
    target.write_text(content, encoding="utf-8")

    fingerprints = manager.fingerprints(worktree, [SLUGIFY_TEST])

    assert fingerprints == {
        SLUGIFY_TEST: hashlib.sha256(content.encode()).hexdigest(),
    }


# ---------------------------------------------------------------------------
# judge_prompt exposes implementer-changed tests (alloy-5wb.3)
# ---------------------------------------------------------------------------


def test_judge_prompt_accepts_changed_tests_and_renders_section():
    assert "changed_tests" in inspect.signature(judge_prompt).parameters

    prompt = judge_prompt(
        "brief",
        "acceptance",
        {},
        "diff",
        [],
        [],
        1,
        "within limits",
        changed_tests=[SLUGIFY_TEST],
    )

    assert IMPLEMENTER_TESTS_HEADING in prompt
    assert _implementer_tests_section(prompt) == SLUGIFY_TEST


# ---------------------------------------------------------------------------
# Integration: acceptance gate prompt
# ---------------------------------------------------------------------------


async def test_acceptance_gate_shows_none_when_only_tests_role_touched_slugify(project, alloy_home, fake_harnesses):
    """tests/test_slugify.py differs from base, but the implementer did not edit it."""
    fake_harnesses.configure(_acceptance_script())
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    prompt = fake_harnesses.calls_for("acceptance")[0]["prompt"]
    assert IMPLEMENTER_TESTS_HEADING in prompt
    assert _implementer_tests_section(prompt) == "(none)"


async def test_acceptance_gate_lists_slugify_when_implementer_rewrites_test(project, alloy_home, fake_harnesses):
    """Only slugify's fingerprint moves; the tests role's helper file stays put."""
    fake_harnesses.configure(
        _acceptance_script(
            tests=write_tests_with_helper_entry(),
            implement=[implement_rewrite_tests_entry()],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    prompt = fake_harnesses.calls_for("acceptance")[0]["prompt"]
    assert _implementer_tests_section(prompt) == SLUGIFY_TEST


async def test_acceptance_gate_lists_only_helper_when_implementer_rewrites_helper_not_slugify(
    project, alloy_home, fake_harnesses
):
    """Slugify stays as the tests role wrote it; only the helper file fingerprint moves."""
    fake_harnesses.configure(
        _acceptance_script(
            tests=write_tests_with_helper_entry(),
            implement=[implement_rewrite_helper_test_entry()],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    prompt = fake_harnesses.calls_for("acceptance")[0]["prompt"]
    assert _implementer_tests_section(prompt) == HELPER_TEST


async def test_acceptance_gate_lists_test_added_by_implementer_after_prove_red(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(_acceptance_script(implement=[implement_add_test_entry()]))
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    prompt = fake_harnesses.calls_for("acceptance")[0]["prompt"]
    assert _implementer_tests_section(prompt) == EXTRA_TEST


# ---------------------------------------------------------------------------
# Integration: judge prompt on escalation
# ---------------------------------------------------------------------------


async def test_judge_lists_slugify_when_implementer_rewrites_test_on_escalation(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        _escalate_to_judge_script(
            tests=write_tests_with_helper_entry(),
            implement=[implement_rewrite_tests_entry()],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    prompt = fake_harnesses.calls_for("judge")[0]["prompt"]
    assert IMPLEMENTER_TESTS_HEADING in prompt
    assert _implementer_tests_section(prompt) == SLUGIFY_TEST


async def test_judge_lists_only_helper_when_implementer_rewrites_helper_on_escalation(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(
        _escalate_to_judge_script(
            tests=write_tests_with_helper_entry(),
            implement=[implement_rewrite_helper_test_entry()],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    prompt = fake_harnesses.calls_for("judge")[0]["prompt"]
    assert IMPLEMENTER_TESTS_HEADING in prompt
    assert _implementer_tests_section(prompt) == HELPER_TEST


async def test_judge_shows_none_when_only_tests_role_touched_slugify_on_escalation(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(_escalate_to_judge_script())
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    prompt = fake_harnesses.calls_for("judge")[0]["prompt"]
    assert IMPLEMENTER_TESTS_HEADING in prompt
    assert _implementer_tests_section(prompt) == "(none)"


# ---------------------------------------------------------------------------
# Attempt history rendering
# ---------------------------------------------------------------------------


async def test_attempt_render_includes_tests_edited_when_implementer_changed_test(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(_acceptance_script(implement=[implement_rewrite_tests_entry()]))
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    rendered = Attempt.model_validate(final["attempts"][0]).render()
    assert f"tests edited: {SLUGIFY_TEST}" in rendered
