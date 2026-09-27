"""Regressions for docs/journal-operating-tentura.md: what surprised the
operator running Alloy day to day against a real target project."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import bd_create
from support import make_bead

from alloy import beads as bd
from alloy.config import MemorySpec
from alloy.engine import MAX_LAND_ATTEMPTS, Engine, EngineError, RunResult
from alloy.models import (
    PINNED_CHECK_HINTS_KEY,
    AcceptanceVerdict,
    AgentResult,
    CheckRequest,
    ProjectMemory,
    ScopeVerdict,
    is_placeholder_text,
    parse_pinned_check_hints,
)
from alloy.procs import pid_alive, pids_under, stop_processes_under
from alloy.recipes import bug_triage
from alloy.recipes.role_prompts import verifier_prompt
from alloy.recipes.shared_verification import classify
from alloy.reconcile import recipe_candidates, reconcile
from alloy.runtime import RunContext
from alloy.scheduler import RunCancelled, Scheduler
from alloy.verify import noop_reason, run_check
from alloy.worktree import SETUP_HOOK, WorktreeManager


@pytest.fixture
def engine(beads_project, alloy_home):
    return Engine.open(beads_project, alloy_home)


def _create(repo: Path, *args: str) -> str:
    proc = subprocess.run(["bd", "create", *args, "--silent"], cwd=repo, check=True, capture_output=True, text=True)
    return proc.stdout.strip().splitlines()[-1].strip()


def _run_record(engine: Engine, bead_id: str, run_id: str, **fields) -> None:
    engine.store.create_run(
        run_id=run_id,
        bead_id=bead_id,
        thread_id=run_id,
        recipe="tdd-loop",
        repo=engine.repo,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    if fields:
        engine.store.update_run(run_id, **fields)


# -- 1: a dispatch hold is re-announced, not logged once ---------------------


def test_a_lasting_dispatch_hold_is_reannounced_as_a_stalled_event(engine, beads_project, caplog):
    now = [datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)]
    scheduler = Scheduler(engine=engine, poll_seconds=0.01, once=True, stall_minutes=30, clock=lambda: now[0])
    holder = bd_create(beads_project, "holder", alloy_recipe="tdd-loop")
    engine.beads.claim(holder)
    engine.beads.set_status(holder, bd.STATUS_REVIEW_READY)
    bd_create(beads_project, "waiting", alloy_recipe="tdd-loop")

    assert scheduler.next_task() is None
    now[0] += timedelta(minutes=10)
    assert scheduler.next_task() is None
    assert not [e for e in engine.store.events.read() if e["event"] == "stalled"]

    now[0] += timedelta(minutes=25)
    assert scheduler.next_task() is None
    now[0] += timedelta(minutes=1)
    assert scheduler.next_task() is None  # same period: no second announcement

    stalled = [e for e in engine.store.events.read() if e["event"] == "stalled"]
    assert len(stalled) == 1
    assert stalled[0]["bead"] == holder
    assert "not landed" in stalled[0]["reason"] and "alloy reconcile" in stalled[0]["reason"]
    assert any("dispatch held by" in r.message and r.levelname == "WARNING" for r in caplog.records)


# -- 3: cancelling a scheduler-owned run leaves the scheduler serving --------


async def test_request_cancel_stops_only_the_current_run(engine, beads_project, monkeypatch):
    scheduler = Scheduler(engine=engine, poll_seconds=0.01)
    started = asyncio.Event()
    cancelled: list[str] = []

    async def slow_run(bead_id, *, recipe_name=None):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(engine, "run", slow_run)
    monkeypatch.setattr(engine, "cancel", lambda bead_id, **_: cancelled.append(bead_id) or True)

    task = asyncio.create_task(scheduler._run_current("b-1"))
    await started.wait()
    assert scheduler.request_cancel("other") is False
    assert scheduler.request_cancel("b-1") is True
    with pytest.raises(RunCancelled):
        await task
    assert cancelled == ["b-1"]
    assert not scheduler._stopping


# -- 2/5: human-operated beads and tracking epics are never dispatched -------


def test_manual_beads_and_bead_markers(engine, beads_project):
    scheduler = Scheduler(engine=engine, poll_seconds=0.01, once=True)
    bd_create(beads_project, "merge gate checklist", alloy_recipe="tdd-loop", alloy_manual="true")
    assert scheduler.next_task() is None
    assert make_bead(labels=["merge-gate"]).manual
    assert make_bead(labels=["manual"]).manual
    assert not make_bead(labels=["backend"]).manual


def test_an_epic_whose_children_all_closed_is_not_run_as_a_task(engine, beads_project):
    scheduler = Scheduler(engine=engine, poll_seconds=0.01, once=True)
    epic = _create(beads_project, "tracking epic", "-t", "epic")
    child = _create(beads_project, "child", "--parent", epic)
    engine.beads.set_metadata(epic, {bd.META_RECIPE: "tdd-loop"})
    engine.beads.close(child)
    assert scheduler.next_task() is None


async def test_manual_beads_are_refused_by_run_and_land(engine, beads_project):
    bead_id = bd_create(beads_project, "gate", alloy_recipe="tdd-loop", alloy_manual="true")
    with pytest.raises(EngineError, match="human-operated"):
        await engine.run(bead_id)
    engine.beads.set_status(bead_id, bd.STATUS_REVIEW_READY)
    with pytest.raises(EngineError, match="human-operated"):
        await engine.land(bead_id)


# -- 2: landing never reopens closed work nor loops forever ------------------


async def test_land_refuses_a_closed_bead(engine, beads_project):
    bead_id = bd_create(beads_project, "done already", alloy_recipe="tdd-loop")
    engine.beads.close(bead_id)
    with pytest.raises(EngineError, match="already closed"):
        await engine.land(bead_id)


async def test_a_branch_with_nothing_to_land_is_closed_without_a_land_run(engine, beads_project, alloy_home):
    bead_id = bd_create(beads_project, "empty branch", alloy_recipe="tdd-loop", alloy_use_worktree="true")
    engine.beads.claim(bead_id)
    engine.beads.set_status(bead_id, bd.STATUS_REVIEW_READY)
    WorktreeManager(repo=beads_project, root=engine.paths.worktrees).ensure(bead_id)

    result = await engine.land(bead_id)

    assert result.outcome == "landed" and result.reason == "nothing to land"
    bead = engine.beads.show(bead_id)
    assert bead.status == bd.STATUS_DONE
    assert engine.store.latest_run_for_bead(bead_id) is None  # no land run was paid for


async def test_an_in_place_bead_lands_against_the_base_its_run_recorded(engine, beads_project, monkeypatch):
    bead_id = bd_create(beads_project, "in place", alloy_recipe="tdd-loop")
    engine.beads.claim(bead_id)
    engine.beads.set_status(bead_id, bd.STATUS_REVIEW_READY)
    worktrees = WorktreeManager(repo=beads_project, root=engine.paths.worktrees)
    base = worktrees.head(beads_project)
    _run_record(engine, bead_id, "work", status="done")
    with engine.store.connect() as conn:
        conn.execute("UPDATE runs SET base_commit = ? WHERE run_id = 'work'", (base,))
    (beads_project / "feature.txt").write_text("work\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=beads_project, check=True)
    subprocess.run(["git", "commit", "-qm", "work"], cwd=beads_project, check=True)

    seen: dict = {}

    async def fake_execute(bead, recipe_name, *, run_id, resume_payload, worktree=None, **_):
        seen["worktree"] = worktree
        return RunResult(bead.id, "land-run", "done", reason="green")

    monkeypatch.setattr(engine, "_execute", fake_execute)
    result = await engine.land(bead_id)

    assert result.outcome == "landed"
    assert seen["worktree"].base_commit == base  # the judge sees the bead's diff, not an empty one


def test_a_failed_land_does_not_reopen_a_bead_closed_meanwhile(engine, beads_project):
    bead_id = bd_create(beads_project, "closed during land", alloy_recipe="tdd-loop")
    bead = engine.beads.show(bead_id)
    engine.beads.close(bead_id)
    config = engine.validate_recipe("land")
    with pytest.raises(EngineError):
        engine._settle_failed_land(bead, RunResult(bead_id, "r", "red", reason="empty diff"), config)
    assert engine.beads.show(bead_id).status == bd.STATUS_DONE


def test_repeated_land_failures_park_for_a_human_instead_of_filing_more_bugs(engine, beads_project):
    bead_id = bd_create(
        beads_project,
        "keeps failing",
        alloy_recipe="tdd-loop",
        **{bd.META_LAND_ATTEMPTS: MAX_LAND_ATTEMPTS - 1},
    )
    engine.beads.claim(bead_id)
    engine.beads.set_status(bead_id, bd.STATUS_REVIEW_READY)
    bead = engine.beads.show(bead_id)
    config = engine.validate_recipe("land")
    with pytest.raises(EngineError, match="parked after"):
        engine._settle_failed_land(bead, RunResult(bead_id, "r", "red", reason="empty diff"), config)
    parked = engine.beads.show(bead_id)
    assert parked.status == bd.STATUS_WAITING_HUMAN
    assert parked.metadata.get(bd.META_LAND_STATE) == "parked"
    assert not parked.metadata.get(bd.META_LAND_REPAIR)


# -- 4/5: assign-recipe retargets pinned beads, never epics or gates ---------


def test_assign_recipe_candidates_skip_epics_manual_and_same_recipe(engine, beads_project):
    pinned = bd_create(beads_project, "pinned", alloy_recipe="tdd-loop")
    bd_create(beads_project, "already there", alloy_recipe="tdd-loop-sonnet")
    bd_create(beads_project, "gate", alloy_recipe="tdd-loop", alloy_manual="true")
    bd_create(beads_project, "unassigned")
    epic = _create(beads_project, "epic", "-t", "epic")
    engine.beads.set_metadata(epic, {bd.META_RECIPE: "tdd-loop"})

    assert [b.id for b in recipe_candidates(engine, "tdd-loop-sonnet")] == [pinned]
    assert recipe_candidates(engine, "tdd-loop-sonnet", from_recipe="tdd-loop-jev") == []


# -- 13: reconcile finds and fixes drift between bd, metadata and runs -------


def test_reconcile_fixes_state_drift(engine, beads_project):
    closed_live = bd_create(beads_project, "closed but running", alloy_recipe="tdd-loop")
    _run_record(engine, closed_live, "live")
    engine.beads.close(closed_live)

    stale = bd_create(beads_project, "stale implementing", alloy_recipe="tdd-loop")
    engine.beads.claim(stale)
    _run_record(engine, stale, "gone", status="cancelled")

    repairing = bd_create(
        beads_project,
        "closed with land state",
        alloy_recipe="tdd-loop",
        **{bd.META_LAND_STATE: "repairing", bd.META_LAND_REPAIR: "x-1"},
    )
    engine.beads.close(repairing)

    gate = bd_create(beads_project, "gate", alloy_recipe="tdd-loop", alloy_manual="true")

    findings = reconcile(engine, [closed_live, stale, repairing, gate])
    assert {f.bead for f in findings} == {closed_live, stale, repairing, gate}
    assert all(f.action is not None for f in findings)

    reconcile(engine, [closed_live, stale, repairing, gate], apply=True)
    assert engine.store.get_run("live")["status"] == "cancelled"
    assert engine.beads.show(closed_live).status == bd.STATUS_DONE
    assert engine.beads.show(stale).status == bd.STATUS_READY
    assert bd.META_LAND_STATE not in engine.beads.show(repairing).metadata
    assert engine.beads.show(gate).recipe is None
    assert reconcile(engine, [closed_live, stale, repairing, gate]) == []


# -- 6: operator-pinned check hints ------------------------------------------


def test_pinned_check_hints_are_parsed_rendered_and_kept_out_of_shared_memory():
    body = (
        "# full web pipeline\nbuild: flutter build web --wasm && dart run tool/apply.dart\n- dart run tool/verify.dart"
    )
    assert parse_pinned_check_hints(body) == [
        "flutter build web --wasm && dart run tool/apply.dart",
        "dart run tool/verify.dart",
    ]
    memory = ProjectMemory.from_raw({PINNED_CHECK_HINTS_KEY: body, "style": "tabs"}, MemorySpec())
    assert "flutter build" not in memory.render() and "tabs" in memory.render()

    prompt = verifier_prompt("brief", "", {}, "", [], [], 1, 3, 10, [], pinned_checks=["dart run tool/verify.dart"])
    assert "Operator-pinned checks (verified by a human)" in prompt
    assert prompt.index("Operator-pinned") < prompt.index("Hints from the repository")


# -- 7: an in-place run says so ----------------------------------------------


def test_in_place_runs_tell_every_role_where_the_work_lives(engine, beads_project):
    bead = make_bead(status="implementing")
    ctx = engine.build_context(bead, "tdd-loop", run_id="r-1", checkpointer=None)
    assert "works in place" in ctx.checkout_note and "`main`" in ctx.checkout_note
    assert ctx.task_brief().startswith(bead.task_brief())
    assert "## Checkout" in ctx.task_brief()

    plain = RunContext(
        bead=bead,
        recipe=None,
        run_id="r",
        worktree=None,
        worktrees=None,
        registry=None,
        store=None,
        checkpointer=None,
        log_dir=Path("."),
    )
    assert plain.task_brief() == bead.task_brief()


# -- 8: placeholder verdicts are asked again ---------------------------------


def _answer(structured: dict) -> AgentResult:
    now = datetime.now(timezone.utc)
    return AgentResult(
        runner="fake", ok=True, exit_code=0, structured=structured, started_at=now, ended_at=now, duration_s=0
    )


class _CallCtx:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    async def call(self, role, spec, prompt, *, schema=None, iteration=0):
        self.calls += 1
        return self.answers.pop(0)


async def test_a_placeholder_verdict_is_asked_again_then_treated_as_a_failure():
    glitch = {"decision": "accept", "reason": "test", "confidence": 0.5}
    good = {"decision": "accept", "reason": "the new test covers the criterion", "confidence": 0.9}

    ctx = _CallCtx([_answer(glitch), _answer(good)])
    default = AcceptanceVerdict(decision="escalate")
    verdict = await classify(ctx, "acceptance", None, "p", model_cls=AcceptanceVerdict, default=default)
    assert verdict.reason == good["reason"] and ctx.calls == 2

    ctx = _CallCtx([_answer(glitch), _answer(glitch)])
    default = AcceptanceVerdict(decision="escalate")
    verdict = await classify(ctx, "acceptance", None, "p", model_cls=AcceptanceVerdict, default=default)
    assert verdict is default and "placeholder" in verdict.reason
    assert is_placeholder_text(" Test. ") and not is_placeholder_text("tests pass")


# -- 9: a no-op command is not a check ---------------------------------------


async def test_echo_is_refused_as_a_check(tmp_path):
    assert noop_reason("echo flutter test -n 4")
    assert noop_reason("FOO=1 true")
    assert not noop_reason("flutter test")
    assert not noop_reason("echo start && flutter test")
    result = await run_check(CheckRequest(command="echo flutter test"), tmp_path)
    assert result.refused and not result.runnable and not result.ok
    assert result.headline().startswith("not a check")


# -- 10: a too-broad rejection names the files the fix needs -----------------


async def test_a_too_broad_rejection_splits_needed_and_incidental_files(monkeypatch):
    async def fake_gate(ctx, bug, diff):
        return ScopeVerdict(verdict="too-broad", reason="helper suite", needed_files=["lib/finder.dart"])

    monkeypatch.setattr(bug_triage, "scope_gate", fake_gate)
    diff = "diff --git a/lib/finder.dart b/lib/finder.dart\n+x\ndiff --git a/tool/ast.dart b/tool/ast.dart\n+y\n"
    ok, reason = await bug_triage.scope_merge_gate(None, None, diff)
    assert not ok
    assert "Needed for the fix: lib/finder.dart" in reason and "not needed: tool/ast.dart" in reason


# -- 11: processes left in a discarded worktree are stopped ------------------


def test_processes_left_in_a_worktree_are_stopped(tmp_path, project):
    tree = tmp_path / "worktrees" / "b-1"
    tree.mkdir(parents=True)
    server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tree)
    try:
        assert server.pid in pids_under(tree)
        assert os.getpid() not in pids_under(Path("/"))
        manager = WorktreeManager(repo=project, root=tmp_path / "worktrees")
        assert manager.stop_processes(project) == []  # the primary checkout is never swept
        assert server.pid in stop_processes_under(tree, grace_s=2)
        server.wait(timeout=5)
        assert not pid_alive(server.pid)
    finally:
        if server.poll() is None:
            server.kill()


# -- 12: a fresh worktree runs the project's setup hook ----------------------


def test_a_fresh_worktree_runs_the_setup_hook(tmp_path, project):
    hook = project / SETUP_HOOK
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text('#!/bin/sh\necho "$ALLOY_PRIMARY_CHECKOUT" > generated.txt\n', encoding="utf-8")
    hook.chmod(0o755)
    worktree = WorktreeManager(repo=project, root=tmp_path / "worktrees").ensure("b-1")
    assert (worktree.path / "generated.txt").read_text(encoding="utf-8").strip() == str(project.resolve())
