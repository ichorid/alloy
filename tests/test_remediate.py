"""Autonomous remediation graph (alloy-0uc.10): parent waits on child, resumes at implement."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from conftest import IMPLEMENTATION, implement_entry, judge_entry
from langgraph.types import Command
from support import RemediationStep, bind_fake_remediator, make_bead, make_harness
from test_triage import (
    RecordingBeadsClient,
    implement_stopped_with_bug,
    script,
    triage_config,
    triage_entry,
)

from alloy.config import Limits
from alloy.models import Outcome


def _agent_roles(fake_harnesses) -> list[str]:
    return [call["role"] for call in fake_harnesses.calls]


def _interrupt_reason(paused: dict) -> str:
    return paused["__interrupt__"][0].value["reason"]


async def test_blocking_bug_remediated_then_parent_resumes_at_implement(project, alloy_home, fake_harnesses):
    """Successful remediation merges a fix and the parent continues without an early implement."""
    beads = RecordingBeadsClient(bug_ids=["bug-fixed"])
    bead = make_bead(id="parent-remed", metadata={"alloy_recipe": "tdd-loop"})
    fake_harnesses.configure(
        script(
            implement=[
                implement_stopped_with_bug("Race in worker pool"),
                implement_entry(succeed=True),
            ],
            triage=[triage_entry("blocking", "reproduces on CI")],
            judge=[judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home, config=triage_config(), bead=bead, beads=beads)
    remediator = bind_fake_remediator(
        harness,
        steps=[
            RemediationStep(
                outcome=Outcome.DONE.value,
                fix_writes=[{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}],
            ),
        ],
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == Outcome.DONE.value
    assert len(remediator.calls) == 1
    assert remediator.calls[0]["bead_id"] == "bug-fixed"

    roles = _agent_roles(fake_harnesses)
    triage_idx = roles.index("triage")
    post_remediate_implement_idx = roles.index("implement", triage_idx + 1)
    assert "implement" not in roles[triage_idx + 1 : post_remediate_implement_idx]

    remediations = final.get("remediations") or []
    assert len(remediations) == 1
    entry = remediations[0]
    assert entry["bead_id"] == "bug-fixed"
    assert entry["outcome"] == Outcome.DONE.value
    assert entry.get("run_id")
    assert entry.get("started_at")
    assert entry.get("ended_at")

    post_remediate_prompt = fake_harnesses.calls_for("implement")[-1]["prompt"]
    assert "bug-fixed" in post_remediate_prompt
    assert "do not undo it" in post_remediate_prompt.lower()


async def test_remediation_waiting_human_parks_parent_then_resumes_at_implement(project, alloy_home, fake_harnesses):
    reason = "child needs architect sign-off"
    beads = RecordingBeadsClient(bug_ids=["bug-wait"])
    fake_harnesses.configure(
        script(
            implement=[
                implement_stopped_with_bug("Schema mismatch"),
                implement_entry(succeed=True),
            ],
            triage=[triage_entry("blocking")],
            judge=[judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home, config=triage_config(), beads=beads)
    remediator = bind_fake_remediator(
        harness,
        steps=[RemediationStep(outcome=Outcome.WAITING_HUMAN.value, reason=reason)],
    )
    try:
        paused = await harness.start()
        assert "__interrupt__" in paused
        interrupt_reason = _interrupt_reason(paused)
        assert reason in interrupt_reason
        assert "bug-wait" in interrupt_reason

        final = await harness.resume(Command(resume={"instructions": "approved"}))
    finally:
        harness.close()

    assert len(remediator.calls) == 1
    roles = _agent_roles(fake_harnesses)
    triage_idx = roles.index("triage")
    assert roles.index("implement", triage_idx + 1) > triage_idx
    assert final["outcome"] == Outcome.DONE.value


async def test_remediation_failed_merge_conflict_parks_parent_then_resumes_at_implement(
    project, alloy_home, fake_harnesses
):
    reason = "merge conflict merging alloy/bug-merge into alloy/parent"
    beads = RecordingBeadsClient(bug_ids=["bug-merge"])
    fake_harnesses.configure(
        script(
            implement=[
                implement_stopped_with_bug("Conflicting fix"),
                implement_entry(succeed=True),
            ],
            triage=[triage_entry("blocking")],
            judge=[judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home, config=triage_config(), beads=beads)
    bind_fake_remediator(
        harness,
        steps=[RemediationStep(outcome=Outcome.FAILED.value, reason=reason)],
    )
    try:
        paused = await harness.start()
        assert "__interrupt__" in paused
        interrupt_reason = _interrupt_reason(paused)
        assert "merge conflict" in interrupt_reason
        assert "bug-merge" in interrupt_reason

        final = await harness.resume(Command(resume={"instructions": "merged by hand"}))
    finally:
        harness.close()

    roles = _agent_roles(fake_harnesses)
    triage_idx = roles.index("triage")
    assert roles.index("implement", triage_idx + 1) > triage_idx
    assert final["outcome"] == Outcome.DONE.value


async def test_remediation_failed_scope_too_broad_parks_parent_then_resumes_at_implement(
    project, alloy_home, fake_harnesses
):
    reason = "merge gate rejected the fix: too-broad: touches unrelated modules"
    beads = RecordingBeadsClient(bug_ids=["bug-scope"])
    fake_harnesses.configure(
        script(
            implement=[
                implement_stopped_with_bug("Over-broad fix"),
                implement_entry(succeed=True),
            ],
            triage=[triage_entry("blocking")],
            judge=[judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home, config=triage_config(), beads=beads)
    bind_fake_remediator(
        harness,
        steps=[RemediationStep(outcome=Outcome.FAILED.value, reason=reason)],
    )
    try:
        paused = await harness.start()
        assert "__interrupt__" in paused
        interrupt_reason = _interrupt_reason(paused)
        assert "too-broad" in interrupt_reason
        assert "bug-scope" in interrupt_reason

        final = await harness.resume(Command(resume={"instructions": "narrowed the fix"}))
    finally:
        harness.close()

    roles = _agent_roles(fake_harnesses)
    triage_idx = roles.index("triage")
    assert roles.index("implement", triage_idx + 1) > triage_idx
    assert final["outcome"] == Outcome.DONE.value


async def test_two_blocking_bugs_remediate_sequentially_and_second_triage_sees_first(
    project, alloy_home, fake_harnesses
):
    beads = RecordingBeadsClient(bug_ids=["bug-one", "bug-two"])
    fake_harnesses.configure(
        script(
            implement=[
                implement_stopped_with_bug("First defect"),
                implement_stopped_with_bug("Second defect"),
                implement_entry(succeed=True),
            ],
            triage=[
                triage_entry("blocking", "first blocking"),
                triage_entry("blocking", "second blocking"),
            ],
            judge=[judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home, config=triage_config(), beads=beads)
    remediator = bind_fake_remediator(
        harness,
        steps=[
            RemediationStep(outcome=Outcome.DONE.value),
            RemediationStep(outcome=Outcome.DONE.value),
        ],
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == Outcome.DONE.value
    assert len(remediator.calls) == 2
    remediations = final.get("remediations") or []
    assert len(remediations) == 2
    assert remediations[0]["bead_id"] == "bug-one"
    assert remediations[0]["outcome"] == Outcome.DONE.value
    assert remediations[1]["bead_id"] == "bug-two"

    triage_prompts = [call["prompt"] for call in fake_harnesses.calls_for("triage")]
    second_prompt = triage_prompts[1]
    assert "bug-one" in second_prompt
    assert Outcome.DONE.value in second_prompt


async def test_child_remediation_wall_time_counts_toward_parent_budget(project, alloy_home, fake_harnesses):
    config = replace(
        triage_config(),
        limits=Limits(
            max_iterations=50,
            max_consiliums=0,
            max_agent_calls=100,
            max_wall_time_minutes=0.02,
        ),
    )
    beads = RecordingBeadsClient(bug_ids=["bug-slow"])
    fake_harnesses.configure(
        script(
            implement=[
                implement_stopped_with_bug("Slow remediation"),
                implement_entry(succeed=False),
            ],
            triage=[triage_entry("blocking")],
            judge=[judge_entry("retry", "still broken")],
        )
    )
    harness = make_harness(project, alloy_home, config=config, beads=beads)
    bind_fake_remediator(
        harness,
        steps=[RemediationStep(outcome=Outcome.DONE.value, sleep_s=2.0)],
    )
    try:
        paused = await harness.start()
    finally:
        harness.close()

    assert "__interrupt__" in paused
    assert paused.get("limit_hit") and "max_wall_time" in paused["limit_hit"]
    reason = _interrupt_reason(paused)
    assert "max_wall_time" in reason


async def test_child_run_blocking_bug_skips_remediator_and_files_unclaimed(project, alloy_home, fake_harnesses):
    beads = RecordingBeadsClient(bug_ids=["bug-depth"])
    bead = make_bead(id="child-task", metadata={"alloy_recipe": "tdd-loop"})
    fake_harnesses.configure(
        script(
            implement=[implement_stopped_with_bug("Nested blocking defect")],
            triage=[triage_entry("blocking", "depth-one rule")],
        )
    )
    harness = make_harness(
        project,
        alloy_home,
        config=triage_config(),
        bead=bead,
        beads=beads,
        parent_run_id="parent-run-depth",
    )
    remediator = bind_fake_remediator(harness)
    try:
        paused = await harness.start()
    finally:
        harness.close()

    assert "__interrupt__" in paused
    assert remediator.calls == []
    assert len(beads.create_bug_calls) == 1
    filed = beads.create_bug_calls[0]
    assert filed["priority"] == 1
    assert filed["claim"] is False
    reason = _interrupt_reason(paused)
    assert "bug-depth" in reason


def test_agents_md_documents_remediation_bounds_with_scope_verdict():
    text = Path(__file__).resolve().parents[1].joinpath("AGENTS.md").read_text(encoding="utf-8")
    assert "remediation" in text
    assert "scope" in text
