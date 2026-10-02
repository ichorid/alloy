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
import time
from collections import Counter
from pathlib import Path

import pytest
from conftest import (
    acceptance_entry,
    bd_create,
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    verifier_run_entry,
    verifier_stop_entry,
    write_tests_entry,
)
from support import await_role, make_harness, wait_for_role

from alloy import beads as bd
from alloy.checkpoints import read_checkpoint
from alloy.engine import Engine
from alloy.store import RUN_RUNNING


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


def verification_recovery_script(marker: Path, **overrides):
    """Verifier runs a quick check, then a sleeping check we can SIGKILL into."""
    quick = verifier_run_entry('sh -c "exit 0"', kind="targeted")
    sleep = verifier_run_entry(
        f'{sys.executable} -c "import pathlib, time; '
        f"pathlib.Path({repr(str(marker))}).write_text('sleeping'); "
        f'time.sleep(120)"',
        kind="regression",
    )
    base = script(
        acceptance=[acceptance_entry("accept")],
        judge=[],
        verifier=[quick, sleep, verifier_stop_entry("checks complete")],
    )
    base.update(overrides)
    return base


def wait_for_file(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.1)
    raise AssertionError(f"{path} never appeared")


async def test_an_interrupted_run_resumes_without_repeating_finished_stages(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script(implement=[{"sleep": 30}]))
    harness = make_harness(project, alloy_home)

    task = asyncio.create_task(harness.start())
    await await_role(fake_harnesses, "implement", timeout=30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [call["role"] for call in fake_harnesses.calls] == [
        "context",
        "estimate",
        "tests",
        "implement",
    ]

    snapshot = read_checkpoint(alloy_home / "workflows.db", harness.thread_id)
    assert snapshot is not None
    assert snapshot["values"]["stage"] == "baseline"  # the last committed step

    fake_harnesses.reset_calls()
    fake_harnesses.configure(script())

    final = await harness.resume(None)

    assert final["outcome"] == "done"
    assert [call["role"] for call in fake_harnesses.calls] == [
        "implement",
        "verifier",
        "acceptance",
        "judge",
        "verifier",
        "acceptance",
        "judge",
        "harvest",
    ]


async def test_the_checkpoint_survives_the_object_that_wrote_it(project, alloy_home, fake_harnesses):
    """Nothing is held in memory between invocations -- resume reads from disk."""
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[judge_entry("human", "need input"), judge_entry("done")],
        )
    )
    first = make_harness(project, alloy_home)
    await first.start()

    from langgraph.types import Command

    second = make_harness(project, alloy_home, run_id=first.run_id)
    final = await second.resume(Command(resume={"instructions": "carry on"}))

    assert final["outcome"] == "done"


async def test_a_killed_process_leaves_an_orphaned_run_that_can_be_adopted(beads_project, alloy_home, fake_harnesses):
    """The reboot path: `alloy run` dies, and a later invocation picks it up."""
    fake_harnesses.configure(script(implement=[{"sleep": 60}]))
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "alloy.cli",
            "run",
            bead_id,
            "--repo",
            str(beads_project),
            "--root",
            str(alloy_home),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    try:
        wait_for_role(fake_harnesses, "implement", timeout=90)
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=30)

    engine = Engine.open(beads_project, alloy_home)
    record = engine.store.latest_run_for_bead(bead_id)
    assert record["status"] == RUN_RUNNING  # nobody got to write an ending
    assert engine.beads.show(bead_id).status == bd.STATUS_IMPLEMENTING

    orphans = engine.store.orphaned_runs()
    assert [run["run_id"] for run in orphans] == [record["run_id"]]

    fake_harnesses.reset_calls()
    fake_harnesses.configure(script())

    result = await engine.run(bead_id)

    assert result.outcome == "done"
    assert result.run_id == record["run_id"]  # the same run, continued
    assert [call["role"] for call in fake_harnesses.calls] == [
        "implement",
        "verifier",
        "acceptance",
        "judge",
        "verifier",
        "acceptance",
        "judge",
        "harvest",
    ]
    assert engine.beads.show(bead_id).status == bd.STATUS_DONE


async def test_restart_can_answer_what_was_running_and_where(beads_project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False)],
            judge=[judge_entry("human", "need a decision")],
        )
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
    assert snapshot["interrupts"]  # it is safe to resume, and how
    assert snapshot["values"]["iteration"] == 1


async def test_killed_mid_check_resumes_without_rerunning_finished_stages_or_checks(
    beads_project, alloy_home, fake_harnesses
):
    """SIGKILL during a sleeping verifier check must resume inside the loop."""
    marker = alloy_home / "check-sleeping.marker"
    marker.unlink(missing_ok=True)
    fake_harnesses.configure(verification_recovery_script(marker))
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "alloy.cli",
            "run",
            bead_id,
            "--repo",
            str(beads_project),
            "--root",
            str(alloy_home),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    try:
        wait_for_role(fake_harnesses, "verifier", timeout=90)
        wait_for_file(marker, timeout=90)
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=30)

    calls_before = len(fake_harnesses.calls)
    engine = Engine.open(beads_project, alloy_home)
    record = engine.store.latest_run_for_bead(bead_id)
    assert record["status"] == RUN_RUNNING
    run_id = record["run_id"]
    log_dir = Path(record["log_dir"])

    fake_harnesses.configure(verification_recovery_script(marker))
    result = await engine.run(bead_id)

    assert result.outcome == "done"
    assert result.run_id == run_id

    roles_after = [call["role"] for call in fake_harnesses.calls[calls_before:]]
    assert "tests" not in roles_after
    assert "implement" not in roles_after

    snapshot = engine.graph_snapshot_for_run(run_id)
    final = snapshot["values"]
    checks = final["checks"]
    assert len([c for c in checks if c["kind"] == "targeted"]) == 1
    assert len([c for c in checks if c["kind"] == "regression"]) == 1
    assert all(count == 1 for count in Counter((c["command"], c["kind"]) for c in checks).values())

    check_logs = list(log_dir.glob("check-*.log"))
    assert len(check_logs) == len(checks) + 1


# -- the claim/create_run race (a `claiming` row always exists to recover from) --


def dead_pid() -> int:
    """The pid of a process that has exited and been reaped."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    return process.pid


async def test_a_stuck_claiming_row_is_discarded_when_bd_never_saw_the_claim(beads_project, alloy_home, fake_harnesses):
    """Crash between creating the row and calling `beads.claim`: bd still says
    `open`, so there is nothing to adopt -- the row is discarded and the bead
    is offered fresh."""
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)
    engine.store.create_claiming_run(
        run_id="stuck-1",
        bead_id=bead_id,
        thread_id="stuck-1",
        recipe="tdd-loop",
        repo=beads_project,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    engine.store.update_run("stuck-1", pid=dead_pid())
    assert engine.beads.show(bead_id).status == bd.STATUS_READY

    result = await engine.run(bead_id)

    assert result.outcome == "done"
    assert engine.store.get_run("stuck-1") is None  # discarded, not left behind
    assert result.run_id != "stuck-1"  # a fresh run, not an adoption
    assert engine.beads.show(bead_id).status == bd.STATUS_DONE


async def test_a_stuck_claiming_row_is_promoted_and_adopted_when_bd_saw_the_claim(
    beads_project, alloy_home, fake_harnesses
):
    """Crash between `beads.claim` succeeding and promoting the row to
    `running`: bd already says `implementing`, so the row is promoted and
    adopted exactly like any other crash survivor -- same run_id continued."""
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)
    assert engine.beads.claim(bead_id) is True
    engine.store.create_claiming_run(
        run_id="stuck-2",
        bead_id=bead_id,
        thread_id="stuck-2",
        recipe="tdd-loop",
        repo=beads_project,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    engine.store.update_run("stuck-2", pid=dead_pid())

    result = await engine.run(bead_id)

    assert result.outcome == "done"
    assert result.run_id == "stuck-2"  # the same run, continued
    record = engine.store.get_run("stuck-2")
    assert record["status"] == "done"
    assert record["worktree"] == str(beads_project)  # filled in at promotion
    assert engine.beads.show(bead_id).status == bd.STATUS_DONE


async def test_recover_resolves_stuck_claiming_runs_before_adopting_orphans(beads_project, alloy_home, fake_harnesses):
    """`Scheduler.recover()` sweeps `claiming` rows too, not just `running` ones."""
    from alloy.scheduler import Scheduler

    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)
    assert engine.beads.claim(bead_id) is True
    engine.store.create_claiming_run(
        run_id="stuck-3",
        bead_id=bead_id,
        thread_id="stuck-3",
        recipe="tdd-loop",
        repo=beads_project,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    engine.store.update_run("stuck-3", pid=dead_pid())

    scheduler = Scheduler(engine=engine, poll_seconds=0.01, once=True)
    recovered = await scheduler.recover()

    assert recovered == [bead_id]
    assert engine.store.get_run("stuck-3")["status"] == "done"
    assert engine.beads.show(bead_id).status == bd.STATUS_DONE


async def test_a_lost_claim_race_discards_the_row_and_raises(beads_project, alloy_home, fake_harnesses, monkeypatch):
    """`beads.claim` losing the CAS race must not leave an orphaned `claiming`
    row behind -- there is nothing for recovery to adopt, so there must be
    nothing left to find."""
    from alloy.engine import EngineError

    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)
    monkeypatch.setattr(engine.beads, "claim", lambda *a, **k: False)

    with pytest.raises(EngineError, match="claimed by someone else"):
        await engine.run(bead_id)

    assert engine.store.latest_run_for_bead(bead_id) is None
