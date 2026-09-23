"""Crash and restart.

Alloy must survive the process going away mid-task: the checkpoint says where it
was, the ledger says which run belongs to the bead, and resuming does not repeat
work that was already paid for.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from alloy import beads as bd
from alloy.checkpoints import read_checkpoint
from alloy.engine import Engine
from alloy.store import RUN_RUNNING
from conftest import (
    bd_create,
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    write_tests_entry,
)
from support import await_role, make_harness, wait_for_role


def script(**overrides):
    base = {
        "context": context_entry(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "judge": [judge_entry("done")],
        "critic": critic_entry(),
        "synthesize": synthesize_entry(),
    }
    base.update(overrides)
    return base


async def test_an_interrupted_run_resumes_without_repeating_finished_stages(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(script(implement=[{"sleep": 30}]))
    harness = make_harness(project, alloy_home)

    task = asyncio.create_task(harness.start())
    await await_role(fake_harnesses, "implement", timeout=30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [call["role"] for call in fake_harnesses.calls] == [
        "context", "estimate", "tests", "implement"
    ]

    snapshot = read_checkpoint(alloy_home / "workflows.db", harness.thread_id)
    assert snapshot is not None
    assert snapshot["values"]["stage"] == "baseline"  # the last committed step

    fake_harnesses.reset_calls()
    fake_harnesses.configure(script())

    final = await harness.resume(None)

    assert final["outcome"] == "done"
    assert [call["role"] for call in fake_harnesses.calls] == ["implement", "verifier", "acceptance", "judge"]


async def test_the_checkpoint_survives_the_object_that_wrote_it(
    project, alloy_home, fake_harnesses
):
    """Nothing is held in memory between invocations -- resume reads from disk."""
    fake_harnesses.configure(
        script(implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
               judge=[judge_entry("human", "need input"), judge_entry("done")])
    )
    first = make_harness(project, alloy_home)
    await first.start()

    from langgraph.types import Command

    second = make_harness(project, alloy_home, run_id=first.run_id)
    final = await second.resume(Command(resume={"instructions": "carry on"}))

    assert final["outcome"] == "done"


async def test_a_killed_process_leaves_an_orphaned_run_that_can_be_adopted(
    beads_project, alloy_home, fake_harnesses
):
    """The reboot path: `alloy run` dies, and a later invocation picks it up."""
    fake_harnesses.configure(script(implement=[{"sleep": 60}]))
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    child = subprocess.Popen(
        [sys.executable, "-m", "alloy.cli", "run", bead_id,
         "--repo", str(beads_project), "--root", str(alloy_home)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    try:
        wait_for_role(fake_harnesses, "implement", timeout=90)
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=30)

    engine = Engine.open(beads_project, alloy_home)
    record = engine.store.latest_run_for_bead(bead_id)
    assert record["status"] == RUN_RUNNING          # nobody got to write an ending
    assert engine.beads.show(bead_id).status == bd.STATUS_IMPLEMENTING

    orphans = engine.store.orphaned_runs()
    assert [run["run_id"] for run in orphans] == [record["run_id"]]

    fake_harnesses.reset_calls()
    fake_harnesses.configure(script())

    result = await engine.run(bead_id)

    assert result.outcome == "done"
    assert result.run_id == record["run_id"]        # the same run, continued
    assert [call["role"] for call in fake_harnesses.calls] == ["implement", "verifier", "acceptance", "judge"]
    assert engine.beads.show(bead_id).status == bd.STATUS_REVIEW_READY


async def test_restart_can_answer_what_was_running_and_where(
    beads_project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(
        script(implement=[implement_entry(succeed=False)],
               judge=[judge_entry("human", "need a decision")])
    )
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    await Engine.open(beads_project, alloy_home).run(bead_id)

    # A brand new process, sharing nothing but the files on disk.
    engine = Engine.open(beads_project, alloy_home)
    record = engine.store.latest_run_for_bead(bead_id)
    snapshot = engine.graph_snapshot(bead_id)

    assert record["bead_id"] == bead_id
    assert record["status"] == "waiting-human"
    assert Path(record["worktree"]).is_dir()
    assert Path(record["log_dir"]).is_dir()
    assert snapshot["checkpoint_id"]
    assert snapshot["interrupts"]                    # it is safe to resume, and how
    assert snapshot["values"]["iteration"] == 1
