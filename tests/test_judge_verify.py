"""The judge's `verify` decision: the diff is complete but checks the criteria
require never ran on this tree. Alloy sends the run back to the verifier with
the judge's commands instead of spending a new implement iteration on "run X"
(tentura 2026-11-04..07: 10 of 21 judge retries were exactly that). Once per
iteration -- a second `verify` in the same iteration is an ordinary retry."""

from __future__ import annotations

from conftest import implement_entry, judge_entry
from support import make_harness
from workflow_support import FULL_SUITE, script

from alloy.models import DECISIONS, JudgeDecision


def test_verify_is_a_judge_decision_agents_may_answer():
    assert "verify" in DECISIONS
    assert "verify" in JudgeDecision.schema_for_agents()["properties"]["decision"]["enum"]


async def test_judge_verify_reruns_checks_without_a_new_implement_round(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=True)],
            judge=[
                judge_entry("verify", "the diff is right; the full suite never ran", FULL_SUITE),
                judge_entry("done"),
            ],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["iteration"] == 1, "a checks-only verdict is not a new iteration"
    assert len(fake_harnesses.calls_for("implement")) == 1
    verifier_prompts = [call["prompt"] for call in fake_harnesses.calls_for("verifier")]
    assert any("checks missing on this tree" in prompt and FULL_SUITE in prompt for prompt in verifier_prompts)


async def test_a_second_verify_in_the_same_iteration_is_a_retry(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=True), implement_entry(succeed=True)],
            judge=[
                judge_entry("verify", "full suite never ran", FULL_SUITE),
                judge_entry("verify", "still want the suite", FULL_SUITE),
                judge_entry("done"),
            ],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert len(fake_harnesses.calls_for("implement")) == 2, "the second verify must fall back to a retry"
    second = fake_harnesses.calls_for("implement")[1]["prompt"]
    assert FULL_SUITE in second
