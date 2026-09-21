"""End to end: a real bead, a real worktree, a real test suite, scripted agents."""

from __future__ import annotations

import pytest

from alloy import beads as bd
from alloy.engine import Engine, EngineError
from conftest import (
    bd_create,
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    write_tests_entry,
)


@pytest.fixture
def engine(beads_project, alloy_home):
    return Engine.open(beads_project, alloy_home)


def script(**overrides):
    base = {
        "context": context_entry(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "judge": [judge_entry("done", "tests pass and the diff is right")],
        "critic": critic_entry(),
        "synthesize": synthesize_entry(),
    }
    base.update(overrides)
    return base


async def test_a_successful_run_updates_the_bead_and_leaves_a_branch(
    engine, beads_project, fake_harnesses
):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    result = await engine.run(bead_id)

    assert result.outcome == "done"
    bead = engine.beads.show(bead_id)
    assert bead.status == bd.STATUS_REVIEW_READY
    assert bead.metadata[bd.META_RUN_ID] == result.run_id
    assert bead.metadata[bd.META_BRANCH] == f"alloy/{bead_id}"
    assert bead.metadata[bd.META_WORKTREE] == result.worktree


async def test_the_bead_is_claimed_before_any_agent_runs(
    engine, beads_project, fake_harnesses
):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    await engine.run(bead_id)

    assert engine.beads.ready() == []  # it is no longer offered to anyone else


async def test_agents_only_ever_touch_the_worktree(engine, beads_project, fake_harnesses):
    from pathlib import Path

    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    result = await engine.run(bead_id)

    assert (beads_project / "mypkg" / "__init__.py").read_text() == ""
    assert "slugify" in (Path(result.worktree) / "mypkg" / "__init__.py").read_text()
    for call in fake_harnesses.calls:
        assert call["cwd"] == result.worktree


async def test_a_run_is_recorded_with_every_agent_call(
    engine, beads_project, fake_harnesses
):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    result = await engine.run(bead_id)
    calls = engine.store.agent_calls(result.run_id)

    assert [call["role"] for call in calls] == ["context", "tests", "implement", "judge"]
    for call in calls:
        assert call["prompt_hash"]
        assert call["log_path"]
        assert call["duration_s"] >= 0
    record = engine.store.get_run(result.run_id)
    assert record["status"] == "done"
    assert record["agent_calls"] == 4


async def test_failure_marks_the_bead_failed_and_keeps_the_worktree(
    engine, beads_project, fake_harnesses
):
    fake_harnesses.configure(
        script(implement=[implement_entry(succeed=False)],
               judge=[judge_entry("abort", "cannot be done as specified")])
    )
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    result = await engine.run(bead_id)

    assert result.outcome == "failed"
    assert engine.beads.show(bead_id).status == bd.STATUS_FAILED
    from pathlib import Path

    assert Path(result.worktree).is_dir()  # left for inspection


async def test_human_gate_parks_the_bead_and_resume_completes_it(
    engine, beads_project, fake_harnesses
):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[judge_entry("human", "which unicode normalization?"), judge_entry("done")],
        )
    )
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    paused = await engine.run(bead_id)

    assert paused.outcome == "waiting-human"
    assert paused.interrupt["reason"] == "which unicode normalization?"
    assert engine.beads.show(bead_id).status == bd.STATUS_WAITING_HUMAN
    assert engine.store.get_run(paused.run_id)["status"] == "waiting-human"

    resumed = await engine.resume(bead_id, "use NFKD")

    assert resumed.outcome == "done"
    assert resumed.run_id == paused.run_id  # same run, not a new one
    assert engine.beads.show(bead_id).status == bd.STATUS_REVIEW_READY
    assert "use NFKD" in fake_harnesses.calls_for("implement")[1]["prompt"]


async def test_a_bead_without_a_recipe_is_refused(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "no recipe here")

    with pytest.raises(EngineError, match="no recipe"):
        await engine.run(bead_id)


async def test_a_bead_that_is_not_ready_is_refused(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")
    engine.beads.set_status(bead_id, bd.STATUS_FAILED)

    with pytest.raises(EngineError, match="only 'open'"):
        await engine.run(bead_id)


async def test_cancel_returns_the_bead_to_ready_and_keeps_the_worktree(
    engine, beads_project, fake_harnesses
):
    fake_harnesses.configure(
        script(implement=[implement_entry(succeed=False)],
               judge=[judge_entry("human", "need a decision")])
    )
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")
    paused = await engine.run(bead_id)

    assert engine.cancel(bead_id) is True

    assert engine.beads.show(bead_id).status == bd.STATUS_READY
    assert engine.store.get_run(paused.run_id)["status"] == "cancelled"
    assert engine.cancel(bead_id) is False


async def test_two_beads_get_independent_worktrees(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(script())
    first = bd_create(beads_project, "first", alloy_recipe="tdd-loop")
    second = bd_create(beads_project, "second", alloy_recipe="tdd-loop")

    first_result = await engine.run(first)
    fake_harnesses.reset_calls()
    second_result = await engine.run(second)

    assert first_result.worktree != second_result.worktree
    assert engine.beads.show(first).metadata[bd.META_BRANCH] == f"alloy/{first}"
    assert engine.beads.show(second).metadata[bd.META_BRANCH] == f"alloy/{second}"


async def test_status_output_is_machine_readable(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    result = await engine.run(bead_id)

    from alloy.cli import _status_row

    row = _status_row(engine, engine.store.get_run(result.run_id))

    assert row["bead"] == bead_id
    assert row["recipe"] == "tdd-loop"
    assert row["status"] == "done"
    assert row["iteration"] == 1
    assert row["max_iterations"] == 5
    assert row["tests"] == "1 passed, 0 failed"
    assert row["runner"] == "astra"
    assert row["elapsed"].endswith("m")


async def test_rerunning_a_cancelled_bead_starts_from_a_clean_graph(
    engine, beads_project, fake_harnesses
):
    """A new run must not inherit the abandoned run's graph state."""
    fake_harnesses.configure(
        script(implement=[implement_entry(succeed=False)],
               judge=[judge_entry("human", "need a decision")])
    )
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    abandoned = await engine.run(bead_id)
    engine.cancel(bead_id)

    fake_harnesses.reset_calls()
    fake_harnesses.configure(script())

    result = await engine.run(bead_id)

    assert result.run_id != abandoned.run_id
    assert result.outcome == "done"
    assert [call["role"] for call in fake_harnesses.calls] == [
        "context", "tests", "implement", "judge"
    ]
    assert engine.store.get_run(result.run_id)["iteration"] == 1
