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
    assert engine.beads.claim(bead_id, run_id="stuck-2") is True
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
    assert engine.beads.claim(bead_id, run_id="stuck-3") is True
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


async def test_a_losing_claimants_stuck_row_is_discarded_not_promoted(beads_project, alloy_home, fake_harnesses):
    """Two processes race to claim the same bead; both create their own
    `claiming` row before either calls `claim()`, and both crash before
    learning the outcome. Only the winner's claim actually landed in bd (with
    its run_id stamped as `alloy_run_id` in the same CAS write) -- bd's bare
    `implementing` status cannot distinguish the two rows, but the stamped
    metadata can. The loser's row must be discarded, not promoted and
    adopted as if it had won."""
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)

    # The winner: its claim actually landed in bd, stamping its run_id.
    assert engine.beads.claim(bead_id, run_id="winner") is True
    engine.store.create_claiming_run(
        run_id="winner",
        bead_id=bead_id,
        thread_id="winner",
        recipe="tdd-loop",
        repo=beads_project,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    engine.store.update_run("winner", pid=dead_pid())

    # The loser: created its own row before losing the CAS, then crashed
    # before `run()` ever got to call `discard_claiming_run` for it.
    engine.store.create_claiming_run(
        run_id="loser",
        bead_id=bead_id,
        thread_id="loser",
        recipe="tdd-loop",
        repo=beads_project,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    engine.store.update_run("loser", pid=dead_pid())

    engine._resolve_stuck_claiming(engine.store.get_run("loser"))

    assert engine.store.get_run("loser") is None  # discarded
    assert engine.store.get_run("winner")["status"] == "claiming"  # untouched by resolving the loser


async def test_a_transient_bd_read_failure_never_discards_the_stuck_row(
    beads_project, alloy_home, fake_harnesses, monkeypatch
):
    """bd being briefly unreachable must not look like "the claim never
    landed" -- that would discard the one row that could later be confirmed
    as the winner. `_resolve_stuck_claiming` must let the read failure
    propagate (the caller decides to retry later), never silently discard."""
    from alloy import beads as bd_module

    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)
    assert engine.beads.claim(bead_id, run_id="r1") is True
    engine.store.create_claiming_run(
        run_id="r1",
        bead_id=bead_id,
        thread_id="r1",
        recipe="tdd-loop",
        repo=beads_project,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    engine.store.update_run("r1", pid=dead_pid())

    def _raise(*a, **k):
        raise bd_module.BeadsError("bd temporarily unreachable")

    monkeypatch.setattr(engine.beads, "show", _raise)

    with pytest.raises(bd_module.BeadsError):
        engine._resolve_stuck_claiming(engine.store.get_run("r1"))

    assert engine.store.get_run("r1")["status"] == "claiming"  # left alone, not discarded


# -- round 3: ownership fencing (local run-row CAS + bd run-id fence) --------


def _stuck_claim(engine: Engine, bead_id: str, run_id: str, *, pid: int) -> None:
    engine.store.create_claiming_run(
        run_id=run_id,
        bead_id=bead_id,
        thread_id=run_id,
        recipe="tdd-loop",
        repo=engine.repo,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    engine.store.update_run(run_id, pid=pid)


def _live_foreign_process() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


async def test_recovery_promotion_never_revives_a_concurrently_cancelled_claim(
    beads_project, alloy_home, fake_harnesses, monkeypatch
):
    """`_resolve_stuck_claiming` reads bd, then promotes. A cancel booking
    the same row cancelled in between must win -- the promotion goes through
    the same claiming-only CAS as the claim-time one, not a plain update."""
    from alloy.store import RUN_CANCELLED

    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)
    assert engine.beads.claim(bead_id, run_id="r1") is True
    _stuck_claim(engine, bead_id, "r1", pid=dead_pid())

    real_show = engine.beads.show

    def show_then_cancel(bid):
        bead = real_show(bid)
        engine.store.finish_run("r1", status=RUN_CANCELLED, outcome="cancelled")  # the racing cancel
        return bead

    monkeypatch.setattr(engine.beads, "show", show_then_cancel)
    engine._resolve_stuck_claiming(engine.store.get_run("r1"))

    assert engine.store.get_run("r1")["status"] == RUN_CANCELLED


async def test_every_dead_claimant_is_resolved_so_a_live_winner_still_refuses(
    beads_project, alloy_home, fake_harnesses
):
    """A live winner plus two crashed losers: resolving only the newest loser
    would expose the other one as `latest` -- dead, not `running` -- and let
    a takeover of the `implementing` bead execute beside the winner."""
    from alloy.engine import EngineError

    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)
    winner = _live_foreign_process()
    try:
        assert engine.beads.claim(bead_id, run_id="winner") is True
        _stuck_claim(engine, bead_id, "winner", pid=winner.pid)
        _stuck_claim(engine, bead_id, "loser-b", pid=dead_pid())
        _stuck_claim(engine, bead_id, "loser-c", pid=dead_pid())

        with pytest.raises(EngineError, match="already being claimed"):
            await engine.run(bead_id)
    finally:
        winner.kill()
        winner.wait(timeout=30)

    assert engine.store.get_run("loser-b") is None
    assert engine.store.get_run("loser-c") is None
    assert engine.store.get_run("winner")["status"] == "claiming"
    assert [r["run_id"] for r in engine.store.runs_for_bead(bead_id)] == ["winner"]
    assert fake_harnesses.calls == []  # nothing executed beside the winner


async def test_a_stale_runs_undelivered_done_never_closes_a_newer_runs_claim(beads_project, alloy_home, fake_harnesses):
    """Real bd: run A's `implementing -> closed` is stuck in the outbox; a
    human reopens the bead and run B claims it. Delivering A's row later must
    leave B's claim alone -- `--if-status implementing` alone would match."""
    from alloy.outbox import deliver_pending

    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)
    assert engine.beads.claim(bead_id, run_id="run-a") is True
    _stuck_claim(engine, bead_id, "run-a", pid=dead_pid())
    engine.store.promote_claiming_run("run-a")
    engine.store.finish_run(
        "run-a",
        status="done",
        outcome="done",
        bead_id=bead_id,
        outbox=[("status", {"status": bd.STATUS_DONE, "if_status": bd.STATUS_IMPLEMENTING})],
    )  # committed locally, never delivered (bd was down)

    engine.beads.set_status(bead_id, bd.STATUS_READY)  # a human reopens it
    assert engine.beads.claim(bead_id, run_id="run-b") is True

    deliver_pending(engine.store, engine.beads, bead_id=bead_id)

    bead = engine.beads.show(bead_id)
    assert bead.status == bd.STATUS_IMPLEMENTING
    assert bead.metadata[bd.META_RUN_ID] == "run-b"
    assert engine.store.pending_bd_updates(bead_id) == []


async def test_a_cancel_during_a_bd_outage_never_reopens_a_humans_later_close(
    beads_project, alloy_home, fake_harnesses, monkeypatch
):
    """The cancel's status read fails (bd down), so it cannot know bd's
    status. Its revert must still never become an unguarded write: a human
    closing the bead before delivery wins."""
    from alloy.outbox import deliver_pending

    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)
    assert engine.beads.claim(bead_id, run_id="r1") is True
    _stuck_claim(engine, bead_id, "r1", pid=dead_pid())
    engine.store.promote_claiming_run("r1")

    def bd_down(*a, **k):
        raise bd.BeadsError("bd temporarily unreachable")

    with monkeypatch.context() as patch:
        patch.setattr(engine.beads, "show", bd_down)
        assert engine.cancel(bead_id) is True
    assert engine.store.get_run("r1")["status"] == "cancelled"
    assert engine.store.pending_bd_updates(bead_id)  # the revert is still queued

    engine.beads.close(bead_id, check=True)  # a human closes it meanwhile
    deliver_pending(engine.store, engine.beads, bead_id=bead_id)

    assert engine.beads.show(bead_id).status == bd.STATUS_DONE


async def test_an_orphan_whose_bead_was_claimed_by_another_run_is_superseded_not_adopted(
    beads_project, alloy_home, fake_harnesses
):
    """bd's `alloy_run_id` names a different run now: continuing the orphan
    would execute beside that owner with all its own bd writes fenced off."""
    from alloy.engine import EngineError

    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)
    assert engine.beads.claim(bead_id, run_id="elsewhere") is True
    _stuck_claim(engine, bead_id, "orphan", pid=dead_pid())
    engine.store.promote_claiming_run("orphan")

    with pytest.raises(EngineError, match="belongs to run elsewhere"):
        await engine.run(bead_id)

    assert engine.store.get_run("orphan")["status"] == "cancelled"
    assert engine.beads.show(bead_id).metadata[bd.META_RUN_ID] == "elsewhere"
    assert fake_harnesses.calls == []
