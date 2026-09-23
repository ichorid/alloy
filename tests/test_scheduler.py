"""The scheduler is deliberately dumb: poll, claim one, run it, repeat."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from alloy import beads as bd
from alloy.models import utcnow
from alloy.engine import Engine
from alloy.scheduler import Scheduler, read_pid
from conftest import (
    bd_create,
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    write_tests_entry,
)
from support import await_role


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


@pytest.fixture
def scheduler(beads_project, alloy_home):
    return Scheduler(engine=Engine.open(beads_project, alloy_home), poll_seconds=0.01, once=True)


def test_the_highest_priority_ready_bead_is_selected(scheduler, beads_project):
    bd_create(beads_project, "low", priority=3, alloy_recipe="tdd-loop")
    urgent = bd_create(beads_project, "urgent", priority=0, alloy_recipe="tdd-loop")
    bd_create(beads_project, "mid", priority=1, alloy_recipe="tdd-loop")

    assert scheduler.next_task().id == urgent


def test_beads_without_a_known_recipe_are_ignored(scheduler, beads_project):
    bd_create(beads_project, "unassigned")
    bd_create(beads_project, "bogus recipe", alloy_recipe="does-not-exist")

    assert scheduler.next_task() is None


def test_selection_makes_no_agent_calls(scheduler, beads_project, fake_harnesses):
    bd_create(beads_project, "task", alloy_recipe="tdd-loop")

    scheduler.next_task()

    assert fake_harnesses.calls == []


async def test_a_tick_claims_and_runs_exactly_one_task(
    scheduler, beads_project, fake_harnesses
):
    fake_harnesses.configure(script())
    first = bd_create(beads_project, "first", priority=0, alloy_recipe="tdd-loop")
    second = bd_create(beads_project, "second", priority=1, alloy_recipe="tdd-loop")

    assert await scheduler.tick() is True

    assert scheduler.engine.beads.show(first).status == bd.STATUS_REVIEW_READY
    assert scheduler.engine.beads.show(second).status == bd.STATUS_READY


async def test_an_empty_queue_is_not_an_error(scheduler, fake_harnesses):
    fake_harnesses.configure(script())
    assert await scheduler.tick() is False


async def test_concurrency_one_means_one(scheduler, beads_project, fake_harnesses):
    """A live run blocks the next claim, so two tasks never share the machine."""
    fake_harnesses.configure(script())
    bd_create(beads_project, "task", alloy_recipe="tdd-loop")
    scheduler.engine.store.create_run(
        run_id="busy", bead_id="other", thread_id="other", recipe="tdd-loop",
        repo=beads_project, worktree=None, branch=None, log_dir=None,
    )  # created with this process's pid, so it counts as alive

    assert await scheduler.tick() is False
    assert fake_harnesses.calls == []


async def test_recover_adopts_runs_whose_process_died(
    scheduler, beads_project, fake_harnesses
):
    """Start a run, lose the process, and let the next scheduler finish it."""
    engine = scheduler.engine
    fake_harnesses.configure(script(implement=[{"sleep": 60}]))
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")

    task = asyncio.create_task(engine.run(bead_id))
    await await_role(fake_harnesses, "implement", timeout=30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = engine.store.latest_run_for_bead(bead_id)
    engine.store.update_run(record["run_id"], pid=dead_pid())
    assert [run["run_id"] for run in engine.store.orphaned_runs()] == [record["run_id"]]

    fake_harnesses.reset_calls()
    fake_harnesses.configure(script())

    recovered = await scheduler.recover()

    assert recovered == [bead_id]
    assert engine.beads.show(bead_id).status == bd.STATUS_REVIEW_READY
    assert [call["role"] for call in fake_harnesses.calls] == ["implement", "verifier", "acceptance", "judge"]


async def test_serve_writes_and_removes_its_pidfile(scheduler):
    await scheduler.serve()
    assert read_pid(scheduler.pidfile) is None


async def test_a_second_scheduler_refuses_to_start(scheduler):
    import subprocess
    import sys

    from alloy.scheduler import SchedulerBusy

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        scheduler.pidfile.parent.mkdir(parents=True, exist_ok=True)
        scheduler.pidfile.write_text(str(holder.pid))

        with pytest.raises(SchedulerBusy):
            await scheduler.serve()
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_a_stale_pidfile_is_cleared(scheduler):
    scheduler.pidfile.parent.mkdir(parents=True, exist_ok=True)
    scheduler.pidfile.write_text(str(dead_pid()))

    assert read_pid(scheduler.pidfile) is None
    assert not scheduler.pidfile.exists()


async def test_recover_reconciles_inflight_before_adopting_orphaned_runs(
    scheduler, beads_project, fake_harnesses, monkeypatch
):
    """Per the plan, `Scheduler.recover()` must reconcile stale `inflight_calls`
    rows before it re-adopts any orphaned run, so a leaked row from the crashed
    process is never attributed to whatever runs next."""
    engine = scheduler.engine
    fake_harnesses.configure(script(implement=[{"sleep": 60}]))
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")

    task = asyncio.create_task(engine.run(bead_id))
    await await_role(fake_harnesses, "implement", timeout=30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = engine.store.latest_run_for_bead(bead_id)
    engine.store.update_run(record["run_id"], pid=dead_pid())

    order: list[str] = []
    original_reconcile = engine.store.reconcile_inflight
    original_orphaned_runs = engine.store.orphaned_runs

    def spy_reconcile():
        order.append("reconcile")
        return original_reconcile()

    def spy_orphaned_runs():
        order.append("orphaned_runs")
        return original_orphaned_runs()

    monkeypatch.setattr(engine.store, "reconcile_inflight", spy_reconcile)
    monkeypatch.setattr(engine.store, "orphaned_runs", spy_orphaned_runs)

    fake_harnesses.reset_calls()
    fake_harnesses.configure(script())
    await scheduler.recover()

    assert "reconcile" in order
    assert "orphaned_runs" in order
    assert order.index("reconcile") < order.index("orphaned_runs")


def dead_pid() -> int:
    """The pid of a process that has exited and been reaped."""
    import subprocess
    import sys

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    return process.pid


# -- timed auto-resume (alloy-5wb.4) --------------------------------------


SESSION_LIMIT_MSG = "You've hit your session limit · resets 1:20am"


async def _park_at_session_limit(engine, beads_project):
    bead_id = bd_create(beads_project, "session limit", alloy_recipe="tdd-loop")
    paused = await engine.run(bead_id)
    assert paused.outcome == "waiting-human"
    return bead_id, paused


async def test_scheduler_tick_auto_resumes_when_retry_at_is_past(
    scheduler, beads_project, fake_harnesses
):
    """One tick resumes a parked run whose retry_at has elapsed and clears it."""
    fake_harnesses.configure(
        script(
            tests=[
                {"exit": 1, "stderr": SESSION_LIMIT_MSG},
                write_tests_entry(),
            ],
        )
    )
    bead_id, paused = await _park_at_session_limit(scheduler.engine, beads_project)
    past = (utcnow() - timedelta(minutes=1)).isoformat()
    scheduler.engine.store.update_run(paused.run_id, retry_at=past)

    # No reset_calls() here: it would also rewind the script counters, and the
    # resumed tests call must reach the second (successful) scripted entry.
    tests_calls_before = len(fake_harnesses.calls_for("tests"))
    assert await scheduler.tick() is True

    assert scheduler.engine.beads.show(bead_id).status == bd.STATUS_REVIEW_READY
    record = scheduler.engine.store.get_run(paused.run_id)
    assert record["status"] == "done"
    assert record.get("retry_at") is None
    assert len(fake_harnesses.calls_for("tests")) > tests_calls_before


async def test_scheduler_tick_leaves_parked_run_when_retry_at_is_future(
    scheduler, beads_project, fake_harnesses
):
    """A future retry_at must not be resumed early."""
    fake_harnesses.configure(
        script(tests=[{"exit": 1, "stderr": SESSION_LIMIT_MSG}])
    )
    bead_id, paused = await _park_at_session_limit(scheduler.engine, beads_project)
    future = (utcnow() + timedelta(hours=1)).isoformat()
    scheduler.engine.store.update_run(paused.run_id, retry_at=future)

    fake_harnesses.reset_calls()
    assert await scheduler.tick() is False

    assert scheduler.engine.beads.show(bead_id).status == bd.STATUS_WAITING_HUMAN
    record = scheduler.engine.store.get_run(paused.run_id)
    assert record["status"] == "waiting-human"
    assert record.get("retry_at") == future
    assert fake_harnesses.calls == []
