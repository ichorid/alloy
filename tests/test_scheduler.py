"""The scheduler is deliberately dumb: poll, claim one, run it, repeat."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alloy import beads as bd
from alloy.config import LandingSpec
from alloy.engine import Engine, EngineError
from alloy.memory_embed import BEGIN_MARKER
from alloy.models import EMBED_KEY, LAST_REVIEW_KEY, MEMORY_REVIEW_LABEL, utcnow
from alloy.scheduler import Scheduler, read_pid
from alloy.store import RUN_FAILED, RUN_RUNNING, RUN_WAITING_HUMAN
from alloy.worktree import Worktree, WorktreeManager, branch_name
from conftest import (
    acceptance_entry,
    bd_create,
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    memory_reviewer_entry,
    synthesize_entry,
    verifier_run_entry,
    verifier_stop_entry,
    write_tests_entry,
)
from support import await_role, load_config, load_land_config


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

    # tdd-loop ships landing.mode auto, so the finished bead is also landed
    # (closed) within the same tick; the second bead is never picked up.
    assert scheduler.engine.beads.show(first).status == bd.STATUS_DONE
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
    assert [call["role"] for call in fake_harnesses.calls] == ["implement", "verifier", "acceptance", "judge", "harvest"]


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


def test_due_human_resume_returns_ready_parked_run_without_retry_at(
    scheduler, beads_project,
):
    bead_id = bd_create(beads_project, "human resume", alloy_recipe="tdd-loop")
    run_id = "human-resume-1"
    scheduler.engine.store.create_run(
        run_id=run_id, bead_id=bead_id, thread_id=run_id, recipe="tdd-loop",
        repo=beads_project, worktree=str(beads_project), branch=f"alloy/{bead_id}",
        log_dir=None,
    )
    scheduler.engine.store.update_run(run_id, status="waiting-human", stage="waiting-human")
    scheduler.engine.beads.set_status(bead_id, bd.STATUS_READY)

    due = scheduler.due_human_resume()
    assert due is not None
    assert due["bead_id"] == bead_id
    assert due.get("retry_at") is None


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


# -- scheduler memory review + embed (alloy-4ef.19) ------------------------


EMBED_STALE_KEY = "alloy:meta:embed-stale"
MEMORY_CLOCK = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def _memory_scheduler(beads_project: Path, alloy_home: Path, *, clock: datetime = MEMORY_CLOCK) -> Scheduler:
    engine = Engine.open(beads_project, alloy_home)
    return Scheduler(
        engine=engine,
        poll_seconds=0.01,
        once=True,
        clock=lambda: clock,
    )


def _configure_memory_reviewer(fake_harnesses) -> None:
    fake_harnesses.configure(
        {
            "memory_reviewer": memory_reviewer_entry(
                verdicts=[{"action": "keep", "key": "conv", "reason": "still accurate"}],
            ),
        }
    )


def _seed_embed_memory(engine: Engine) -> None:
    engine.beads.remember(EMBED_KEY, json.dumps(["conv"]))
    engine.beads.remember("conv", "repo uses pathlib")


def _commit_agents(repo: Path) -> Path:
    agents = repo / "AGENTS.md"
    agents.write_text("# Agents\n\nFollow these rules.\n", encoding="utf-8")
    _git(repo, "add", "AGENTS.md")
    _git(repo, "commit", "-qm", "add AGENTS.md")
    return agents


def _seed_finished_runs_since_review(store, count: int) -> None:
    store.set_finished_runs_since_last_review(count)


def _memory_reviewer_calls(fake_harnesses) -> list[dict]:
    return fake_harnesses.calls_for("memory_reviewer")


def _bead_notes(engine: Engine, bead_id: str) -> str:
    rows = engine.beads._json(["show", bead_id, "--json"])
    return str(rows[0].get("notes") or "")


@pytest.fixture
def memory_scheduler_setup(beads_project, alloy_home, fake_harnesses):
    """Scheduler with injected clock and a scripted memory_reviewer harness."""
    _configure_memory_reviewer(fake_harnesses)
    scheduler = _memory_scheduler(beads_project, alloy_home)
    _seed_embed_memory(scheduler.engine)
    agents = _commit_agents(beads_project)
    return scheduler, agents


async def test_scheduler_tick_runs_memory_review_and_embed_when_last_review_is_stale_by_days(
    memory_scheduler_setup, fake_harnesses,
):
    scheduler, agents = memory_scheduler_setup
    stale = (MEMORY_CLOCK.date() - timedelta(days=8)).isoformat()
    scheduler.engine.beads.remember(LAST_REVIEW_KEY, stale)

    assert await scheduler.tick() is True
    assert len(_memory_reviewer_calls(fake_harnesses)) == 1
    assert scheduler.engine.beads.memories()[LAST_REVIEW_KEY] == MEMORY_CLOCK.date().isoformat()
    assert BEGIN_MARKER in agents.read_text(encoding="utf-8")


async def test_scheduler_tick_does_not_repeat_memory_review_same_calendar_day(
    memory_scheduler_setup, fake_harnesses,
):
    scheduler, _agents = memory_scheduler_setup
    stale = (MEMORY_CLOCK.date() - timedelta(days=8)).isoformat()
    scheduler.engine.beads.remember(LAST_REVIEW_KEY, stale)

    assert await scheduler.tick() is True
    assert len(_memory_reviewer_calls(fake_harnesses)) == 1

    assert await scheduler.tick() is False
    assert len(_memory_reviewer_calls(fake_harnesses)) == 1


async def test_scheduler_tick_runs_memory_review_when_finished_runs_exceed_threshold(
    beads_project, alloy_home, fake_harnesses,
):
    _configure_memory_reviewer(fake_harnesses)
    scheduler = _memory_scheduler(beads_project, alloy_home)
    _seed_embed_memory(scheduler.engine)
    _commit_agents(beads_project)
    recent = (MEMORY_CLOCK.date() - timedelta(days=1)).isoformat()
    scheduler.engine.beads.remember(LAST_REVIEW_KEY, recent)
    _seed_finished_runs_since_review(scheduler.engine.store, 21)

    assert await scheduler.tick() is True
    assert len(_memory_reviewer_calls(fake_harnesses)) == 1
    assert scheduler.engine.beads.memories()[LAST_REVIEW_KEY] == MEMORY_CLOCK.date().isoformat()


async def test_scheduler_tick_skips_memory_review_when_not_due_by_days_or_runs(
    beads_project, alloy_home, fake_harnesses,
):
    _configure_memory_reviewer(fake_harnesses)
    scheduler = _memory_scheduler(beads_project, alloy_home)
    _seed_embed_memory(scheduler.engine)
    _commit_agents(beads_project)
    recent = (MEMORY_CLOCK.date() - timedelta(days=1)).isoformat()
    scheduler.engine.beads.remember(LAST_REVIEW_KEY, recent)
    _seed_finished_runs_since_review(scheduler.engine.store, 5)

    assert await scheduler.tick() is False
    assert _memory_reviewer_calls(fake_harnesses) == []
    assert scheduler.engine.beads.memories()[LAST_REVIEW_KEY] == recent


async def test_scheduler_tick_runs_memory_review_when_embed_stale_flag_set(
    beads_project, alloy_home, fake_harnesses,
):
    _configure_memory_reviewer(fake_harnesses)
    scheduler = _memory_scheduler(beads_project, alloy_home)
    _seed_embed_memory(scheduler.engine)
    _commit_agents(beads_project)
    scheduler.engine.beads.remember(LAST_REVIEW_KEY, MEMORY_CLOCK.date().isoformat())
    scheduler.engine.beads.remember(EMBED_STALE_KEY, "true")
    _seed_finished_runs_since_review(scheduler.engine.store, 0)

    assert await scheduler.tick() is True
    assert len(_memory_reviewer_calls(fake_harnesses)) == 1


async def test_scheduler_tick_skips_memory_review_while_a_run_is_active(
    beads_project, alloy_home, fake_harnesses,
):
    _configure_memory_reviewer(fake_harnesses)
    scheduler = _memory_scheduler(beads_project, alloy_home)
    stale = (MEMORY_CLOCK.date() - timedelta(days=8)).isoformat()
    scheduler.engine.beads.remember(LAST_REVIEW_KEY, stale)
    scheduler.engine.store.create_run(
        run_id="busy",
        bead_id="other",
        thread_id="other",
        recipe="tdd-loop",
        repo=beads_project,
        worktree=None,
        branch=None,
        log_dir=None,
    )

    assert await scheduler.tick() is False
    assert _memory_reviewer_calls(fake_harnesses) == []
    assert scheduler.engine.beads.memories()[LAST_REVIEW_KEY] == stale
    assert scheduler.engine.store.get_run("busy")["status"] == RUN_RUNNING


async def test_scheduler_tick_skips_embed_and_notes_uncommitted_when_instruction_file_dirty(
    memory_scheduler_setup, fake_harnesses,
):
    scheduler, agents = memory_scheduler_setup
    stale = (MEMORY_CLOCK.date() - timedelta(days=8)).isoformat()
    scheduler.engine.beads.remember(LAST_REVIEW_KEY, stale)
    agents.write_text(agents.read_text(encoding="utf-8") + "\n# uncommitted edit\n", encoding="utf-8")

    assert await scheduler.tick() is True
    assert len(_memory_reviewer_calls(fake_harnesses)) == 1
    assert scheduler.engine.beads.memories()[LAST_REVIEW_KEY] == MEMORY_CLOCK.date().isoformat()
    assert BEGIN_MARKER not in agents.read_text(encoding="utf-8")

    review_beads = scheduler.engine.beads.open_by_label(MEMORY_REVIEW_LABEL)
    assert review_beads
    notes = _bead_notes(scheduler.engine, review_beads[0].id)
    assert notes.count("uncommitted") == 1


# -- default recipe from memory (alloy-4ef.14) ---------------------------


DEFAULT_RECIPE_KEY = "alloy:default:recipe"


def test_next_task_selects_unassigned_bead_when_default_recipe_memory_set(
    scheduler, beads_project,
):
    bead_id = bd_create(beads_project, "needs default recipe")
    scheduler.engine.beads.remember(DEFAULT_RECIPE_KEY, "tdd-loop")

    picked = scheduler.next_task()

    assert picked is not None
    assert picked.id == bead_id
    assert picked.recipe is None


def test_next_task_skips_unassigned_bead_without_default_recipe_memory(
    scheduler, beads_project,
):
    bd_create(beads_project, "needs default recipe")

    assert scheduler.next_task() is None


def test_next_task_skips_unassigned_bead_when_default_recipe_is_unknown(
    scheduler, beads_project,
):
    bd_create(beads_project, "needs default recipe")
    scheduler.engine.beads.remember(DEFAULT_RECIPE_KEY, "no-such-recipe")

    assert scheduler.next_task() is None


def test_next_task_logs_once_when_default_recipe_is_unknown(
    scheduler, beads_project, caplog,
):
    bd_create(beads_project, "needs default recipe")
    scheduler.engine.beads.remember(DEFAULT_RECIPE_KEY, "no-such-recipe")

    with caplog.at_level(logging.INFO, logger="alloy.scheduler"):
        assert scheduler.next_task() is None
        assert scheduler.next_task() is None

    matches = [record for record in caplog.records if "no-such-recipe" in record.message]
    assert len(matches) == 1


async def test_tick_runs_unassigned_bead_when_default_recipe_memory_set(
    scheduler, beads_project, fake_harnesses,
):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "needs default recipe")
    scheduler.engine.beads.remember(DEFAULT_RECIPE_KEY, "tdd-loop")

    assert await scheduler.tick() is True

    bead = scheduler.engine.beads.show(bead_id)
    # tdd-loop ships landing.mode auto, so the successful run is landed (closed).
    assert bead.status == bd.STATUS_DONE
    assert bead.recipe == "tdd-loop"


async def test_default_recipe_tick_uses_yaml_limits_not_memory(
    scheduler, beads_project, fake_harnesses, monkeypatch,
):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "needs default recipe")
    scheduler.engine.beads.remember(DEFAULT_RECIPE_KEY, "tdd-loop")
    scheduler.engine.beads.remember("alloy:limits:max_agent_calls", "999")

    configs_seen: list = []
    original_load_config = scheduler.engine.load_config

    def spy_load_config(name: str):
        config = original_load_config(name)
        configs_seen.append(config)
        return config

    monkeypatch.setattr(scheduler.engine, "load_config", spy_load_config)

    assert await scheduler.tick() is True

    yaml_limits = load_config().limits
    assert configs_seen
    assert configs_seen[0].limits == yaml_limits
    assert yaml_limits.max_agent_calls == 20
    assert scheduler.engine.beads.show(bead_id).recipe == "tdd-loop"


# -- epic serialization for auto-land (alloy-vrh.6) -----------------------


def _create_epic(repo: Path, title: str) -> str:
    proc = subprocess.run(
        ["bd", "create", title, "-t", "epic", "--silent"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip().splitlines()[-1].strip()


def _create_epic_child(
    repo: Path,
    title: str,
    parent_id: str,
    *,
    priority: int = 2,
    **metadata,
) -> str:
    proc = subprocess.run(
        ["bd", "create", title, "--parent", parent_id, "--silent"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    bead_id = proc.stdout.strip().splitlines()[-1].strip()
    args = ["bd", "update", bead_id, "-p", str(priority)]
    for key, value in metadata.items():
        args += ["--set-metadata", f"{key}={value}"]
    subprocess.run(args, cwd=str(repo), check=True, capture_output=True, text=True)
    return bead_id


def _seed_parked_run(
    scheduler: Scheduler,
    repo: Path,
    bead_id: str,
    run_id: str,
    *,
    status: str = RUN_WAITING_HUMAN,
    worktree: Path | None = None,
) -> None:
    worktree_path = worktree or repo
    scheduler.engine.store.create_run(
        run_id=run_id,
        bead_id=bead_id,
        thread_id=run_id,
        recipe="tdd-loop",
        repo=repo,
        worktree=str(worktree_path),
        branch=f"alloy/{bead_id}",
        log_dir=None,
    )
    scheduler.engine.store.update_run(run_id, status=status, stage=status)


def test_next_task_skips_everything_else_when_epic_child_has_waiting_human_run(
    scheduler, beads_project,
):
    epic_id = _create_epic(beads_project, "shared epic worktree")
    child_x = _create_epic_child(
        beads_project, "epic child X", epic_id, priority=0, alloy_recipe="tdd-loop",
    )
    child_y = _create_epic_child(
        beads_project, "epic child Y", epic_id, priority=1, alloy_recipe="tdd-loop",
    )
    bd_create(beads_project, "standalone S", priority=2, alloy_recipe="tdd-loop")
    _seed_parked_run(scheduler, beads_project, child_x, "epic-x-waiting")

    # Neither the sibling nor an unrelated standalone bead may start while the
    # epic holds a parked run: top-level units run strictly one at a time.
    assert scheduler.next_task() is None


def test_next_task_never_dispatches_epic_itself_while_child_run_is_parked(
    scheduler, beads_project,
):
    epic_id = _create_epic(beads_project, "epic with paused child")
    subprocess.run(
        ["bd", "update", epic_id, "--set-metadata", "alloy_recipe=tdd-loop"],
        cwd=str(beads_project), check=True, capture_output=True, text=True,
    )
    child = _create_epic_child(
        beads_project, "paused child", epic_id, priority=1, alloy_recipe="tdd-loop",
    )
    _seed_parked_run(scheduler, beads_project, child, "epic-child-paused")

    picked = scheduler.next_task()
    assert picked is None or picked.id != epic_id


def test_next_task_never_dispatches_epic_with_open_children_and_no_runs_yet(
    scheduler, beads_project,
):
    epic_id = _create_epic(beads_project, "epic with idle children")
    subprocess.run(
        ["bd", "update", epic_id, "--set-metadata", "alloy_recipe=tdd-loop"],
        cwd=str(beads_project), check=True, capture_output=True, text=True,
    )
    child = _create_epic_child(
        beads_project, "idle child", epic_id, priority=1, alloy_recipe="tdd-loop",
    )
    # The children wait on another epic's bead, so the epic alone is "ready".
    gate = subprocess.run(
        ["bd", "create", "cross-epic gate", "--silent"],
        cwd=str(beads_project), check=True, capture_output=True, text=True,
    ).stdout.strip().splitlines()[-1].strip()
    subprocess.run(
        ["bd", "dep", "add", child, gate],
        cwd=str(beads_project), check=True, capture_output=True, text=True,
    )

    picked = scheduler.next_task()
    assert picked is None or picked.id not in (epic_id, child)


def test_next_task_returns_next_epic_child_after_blocking_sibling_run_is_done(
    scheduler, beads_project,
):
    epic_id = _create_epic(beads_project, "serial epic children")
    child_x = _create_epic_child(
        beads_project, "first epic child", epic_id, priority=0, alloy_recipe="tdd-loop",
    )
    child_y = _create_epic_child(
        beads_project, "second epic child", epic_id, priority=1, alloy_recipe="tdd-loop",
    )
    run_id = "epic-x-done"
    _seed_parked_run(scheduler, beads_project, child_x, run_id)
    scheduler.engine.store.update_run(run_id, status="done", stage="done")
    scheduler.engine.beads.set_status(child_x, bd.STATUS_DONE)

    picked = scheduler.next_task()

    assert picked is not None
    assert picked.id == child_y


def test_next_task_logs_once_when_epic_sibling_blocks_dispatch(
    scheduler, beads_project, caplog,
):
    epic_id = _create_epic(beads_project, "blocked epic")
    child_x = _create_epic_child(
        beads_project, "blocking sibling", epic_id, priority=0, alloy_recipe="tdd-loop",
    )
    _create_epic_child(
        beads_project, "blocked sibling", epic_id, priority=1, alloy_recipe="tdd-loop",
    )
    _seed_parked_run(scheduler, beads_project, child_x, "epic-block-log")

    with caplog.at_level(logging.INFO, logger="alloy.scheduler"):
        assert scheduler.next_task() is None
        assert scheduler.next_task() is None

    matches = [record for record in caplog.records if child_x in record.message]
    assert len(matches) == 1


def test_next_task_skips_epic_children_when_sibling_has_failed_run_with_dirty_worktree(
    scheduler, beads_project,
):
    epic_id = _create_epic(beads_project, "dirty epic worktree")
    child_x = _create_epic_child(
        beads_project, "failed sibling", epic_id, priority=0, alloy_recipe="tdd-loop",
    )
    child_y = _create_epic_child(
        beads_project, "waiting sibling", epic_id, priority=1, alloy_recipe="tdd-loop",
    )
    standalone = bd_create(beads_project, "standalone after dirty fail", priority=2, alloy_recipe="tdd-loop")
    dirty_marker = beads_project / "epic-dirty-marker.txt"
    dirty_marker.write_text("uncommitted epic work\n", encoding="utf-8")
    _seed_parked_run(
        scheduler, beads_project, child_x, "epic-x-failed-dirty", status=RUN_FAILED,
    )

    picked = scheduler.next_task()

    assert picked is not None
    assert picked.id == standalone
    assert picked.id not in {child_x, child_y}


# -- scheduler auto-land (alloy-vrh.10) -----------------------------------


FULL_SUITE = f"{sys.executable} -m pytest -q"


def _patch_engine_recipes(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    *,
    landing_off: bool = False,
) -> None:
    def load(name: str):
        if name == "land":
            return load_land_config()
        config = load_config()
        if landing_off:
            config = replace(config, landing=LandingSpec(mode="off", target="main"))
        return config

    monkeypatch.setattr(engine, "load_config", load)


def _land_harness_entries(**overrides):
    base = {
        "verifier": [
            verifier_run_entry(FULL_SUITE, kind="regression"),
            verifier_stop_entry("regression suite green on merged tree"),
        ],
        "acceptance": [acceptance_entry("accept", confidence=0.9)],
        "judge": [judge_entry("done")],
    }
    base.update(overrides)
    return base


def _auto_land_script(**overrides):
    combined = script(**overrides)
    combined.update(_land_harness_entries())
    return combined


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc


def _head(cwd: Path) -> str:
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


def _head_parent_count(cwd: Path) -> int:
    parts = _git(cwd, "rev-list", "--parents", "-n", "1", "HEAD").stdout.strip().split()
    return len(parts) - 1


def _advance_main(repo: Path) -> str:
    marker = repo / "main-advance.txt"
    marker.write_text("main-only\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "advance main for landing")
    return _head(repo)


def _prepare_merge_conflict(
    project: Path,
    worktree: Worktree,
    conflict_path: str = "mypkg/__init__.py",
) -> str:
    (worktree.path / conflict_path).write_text("bead = 1\n", encoding="utf-8")
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", "bead change")
    (project / conflict_path).write_text("main = 2\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "main change")
    return _head(worktree.path)


def _seed_review_ready(
    engine: Engine,
    beads_project: Path,
    alloy_home: Path,
    bead_id: str,
) -> Worktree:
    engine.beads.claim(bead_id)
    engine.beads.set_status(bead_id, bd.STATUS_REVIEW_READY)
    worktrees = WorktreeManager(repo=beads_project, root=alloy_home / "worktrees")
    worktree = worktrees.ensure(bead_id)
    engine.beads.set_metadata(
        bead_id,
        {
            bd.META_WORKTREE: str(worktree.path),
            bd.META_BRANCH: branch_name(bead_id),
        },
    )
    return worktree


async def test_scheduler_tick_auto_lands_standalone_tdd_loop_after_success(
    scheduler, beads_project, alloy_home, fake_harnesses, monkeypatch,
):
    """Standalone bead with landing.mode auto closes with a merge commit on main after one tick."""
    _patch_engine_recipes(monkeypatch, scheduler.engine)
    fake_harnesses.configure(_auto_land_script())
    bead_id = bd_create(beads_project, "standalone auto land", alloy_recipe="tdd-loop")
    primary_before = _head(beads_project)
    _advance_main(beads_project)

    assert await scheduler.tick() is True

    bead = scheduler.engine.beads.show(bead_id)
    assert bead.status == bd.STATUS_DONE
    assert bead.metadata.get(bd.META_LAND_STATE) == "landed"
    assert bead.metadata.get(bd.META_LAND_SHA)
    assert _head(beads_project) != primary_before
    assert _head_parent_count(beads_project) >= 2


async def test_scheduler_tick_lands_completed_epic_on_next_tick(
    scheduler, beads_project, alloy_home, fake_harnesses, monkeypatch,
):
    """When an epic's last child closes, the next tick lands the epic into main."""
    _patch_engine_recipes(monkeypatch, scheduler.engine)
    fake_harnesses.configure(script())
    epic_id = _create_epic(beads_project, "auto land epic")
    child_id = _create_epic_child(
        beads_project, "only epic child", epic_id, priority=0, alloy_recipe="tdd-loop",
    )

    await scheduler.engine.run(child_id)
    assert scheduler.engine.beads.show(child_id).status == bd.STATUS_DONE
    assert scheduler.engine.beads.show(epic_id).status != bd.STATUS_DONE

    primary_before = _head(beads_project)
    _advance_main(beads_project)
    epic_tip = _head(alloy_home / "worktrees" / epic_id)
    fake_harnesses.reset_calls()
    fake_harnesses.configure(_land_harness_entries())
    assert await scheduler.tick() is True

    epic = scheduler.engine.beads.show(epic_id)
    assert epic.status == bd.STATUS_DONE
    assert epic.metadata.get(bd.META_LAND_STATE) == "landed"
    assert _head(beads_project) != primary_before
    assert _head_parent_count(beads_project) >= 2
    merge_parents = _git(
        beads_project, "rev-list", "--parents", "-n", "1", "HEAD",
    ).stdout.strip().split()[1:]
    # The merge's second parent is the trial-merge commit the land recipe
    # verified on alloy/E -- the branch itself is deleted after landing
    # (docs/plans/auto-land.md), so the epic's own tip arrives as that
    # commit's parent, not as a branch name (same pattern as test_cli_land.py).
    trial_merge_parents = _git(
        beads_project, "rev-list", "--parents", "-n", "1", merge_parents[1],
    ).stdout.strip().split()[1:]
    assert epic_tip in trial_merge_parents


async def test_scheduler_tick_relands_bead_in_repairing_when_repair_bug_closed(
    scheduler, beads_project, alloy_home, fake_harnesses, monkeypatch,
):
    """A review-ready bead in repairing state is re-landed once its repair bug closes."""
    _patch_engine_recipes(monkeypatch, scheduler.engine)
    fake_harnesses.configure(_land_harness_entries())
    conflict_path = "mypkg/__init__.py"
    bead_id = bd_create(beads_project, "repair retry auto land", alloy_recipe="tdd-loop")
    worktree = _seed_review_ready(scheduler.engine, beads_project, alloy_home, bead_id)
    primary_before = _head(beads_project)
    _prepare_merge_conflict(beads_project, worktree, conflict_path)

    with pytest.raises(EngineError):
        await scheduler.engine.land(bead_id)

    landed = scheduler.engine.beads.show(bead_id)
    assert landed.status == bd.STATUS_REVIEW_READY
    assert landed.metadata.get(bd.META_LAND_STATE) == "repairing"
    bug_id = landed.metadata.get(bd.META_LAND_REPAIR)
    assert bug_id

    # Simulate the repair bug's work the way its acceptance criterion demands:
    # merge main into the branch and resolve the conflict, leaving a green
    # suite on the merged tree (a bare commit without the merge would conflict
    # again at trial-merge time, and without a test file pytest exits 5).
    _git(worktree.path, "merge", "--no-edit", "main", check=False)
    (worktree.path / conflict_path).write_text("resolved = 1\n", encoding="utf-8")
    (worktree.path / "tests").mkdir(exist_ok=True)
    (worktree.path / "tests" / "test_placeholder.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8",
    )
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", "resolve landing conflict")
    scheduler.engine.beads.set_status(bug_id, bd.STATUS_DONE)

    fake_harnesses.reset_calls()
    fake_harnesses.configure(_land_harness_entries())
    assert await scheduler.tick() is True

    bead = scheduler.engine.beads.show(bead_id)
    assert bead.status == bd.STATUS_DONE
    assert bead.metadata.get(bd.META_LAND_STATE) == "landed"
    assert _head(beads_project) != primary_before


async def test_scheduler_tick_leaves_landing_off_bead_review_ready(
    scheduler, beads_project, fake_harnesses, monkeypatch,
):
    """A bead whose finished recipe has landing.mode off stays review-ready; main unchanged."""
    import inspect

    from alloy.scheduler import Scheduler as SchedulerClass

    assert ".land(" in inspect.getsource(SchedulerClass.tick), (
        "scheduler.tick must wire auto-land before landing.mode off is testable"
    )
    _patch_engine_recipes(monkeypatch, scheduler.engine, landing_off=True)
    land_calls: list[str] = []
    original_land = scheduler.engine.land

    async def track_land(bead_id: str):
        land_calls.append(bead_id)
        return await original_land(bead_id)

    monkeypatch.setattr(scheduler.engine, "land", track_land)
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "no auto land", alloy_recipe="tdd-loop")
    primary_before = _head(beads_project)

    assert await scheduler.tick() is True

    bead = scheduler.engine.beads.show(bead_id)
    assert bead.status == bd.STATUS_REVIEW_READY
    assert bead.metadata.get(bd.META_LAND_STATE) in (None, "")
    assert land_calls == []
    assert _head(beads_project) == primary_before


async def test_scheduler_auto_land_disabled_by_root_flag_file(
    scheduler, beads_project, fake_harnesses, monkeypatch,
):
    """``<alloy-root>/disable-auto-land`` skips auto-landing even when mode is auto."""
    _patch_engine_recipes(monkeypatch, scheduler.engine, landing_off=False)
    disable = scheduler.engine.paths.root / "disable-auto-land"
    disable.write_text("", encoding="utf-8")
    land_calls: list[str] = []
    original_land = scheduler.engine.land

    async def track_land(bead_id: str):
        land_calls.append(bead_id)
        return await original_land(bead_id)

    monkeypatch.setattr(scheduler.engine, "land", track_land)
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "flag disables auto land", alloy_recipe="tdd-loop")

    assert await scheduler.tick() is True

    assert scheduler.engine.beads.show(bead_id).status == bd.STATUS_REVIEW_READY
    assert land_calls == []
    disable.unlink(missing_ok=True)


# -- strict top-level serialization ------------------------------------------


def _park_run(scheduler, beads_project, bead_id, status=RUN_WAITING_HUMAN):
    run_id = f"run-{bead_id}"
    scheduler.engine.store.create_run(
        run_id=run_id, bead_id=bead_id, thread_id=run_id, recipe="tdd-loop",
        repo=scheduler.engine.repo, worktree=str(beads_project), branch=f"alloy/{bead_id}",
        log_dir=None,
    )
    scheduler.engine.store.update_run(run_id, status=status, stage=status)


def test_a_waiting_human_bead_blocks_independent_beads(scheduler, beads_project):
    stuck = bd_create(beads_project, "stuck", priority=0, alloy_recipe="tdd-loop")
    bd_create(beads_project, "other", priority=1, alloy_recipe="tdd-loop")
    _park_run(scheduler, beads_project, stuck)
    scheduler.engine.beads.set_status(stuck, bd.STATUS_WAITING_HUMAN)

    assert scheduler.next_task() is None


def test_a_running_bead_blocks_other_top_level_units(scheduler, beads_project):
    busy = bd_create(beads_project, "busy", alloy_recipe="tdd-loop")
    bd_create(beads_project, "other", alloy_recipe="tdd-loop")
    _park_run(scheduler, beads_project, busy, status=RUN_RUNNING)
    scheduler.engine.beads.set_status(busy, bd.STATUS_IMPLEMENTING)

    assert scheduler.next_task() is None


def test_a_review_ready_bead_blocks_other_top_level_units(scheduler, beads_project):
    held = bd_create(beads_project, "held", alloy_recipe="tdd-loop")
    bd_create(beads_project, "other", alloy_recipe="tdd-loop")
    scheduler.engine.beads.set_status(held, bd.STATUS_REVIEW_READY)

    assert scheduler.next_task() is None


def test_the_repair_bug_of_a_review_ready_bead_is_not_blocked(scheduler, beads_project):
    held = bd_create(beads_project, "held", alloy_recipe="tdd-loop")
    bug = bd_create(beads_project, "repair", alloy_recipe="tdd-loop")
    scheduler.engine.beads.set_status(held, bd.STATUS_REVIEW_READY)
    scheduler.engine.beads.set_metadata(held, {bd.META_LAND_REPAIR: bug})

    picked = scheduler.next_task()

    assert picked is not None and picked.id == bug


def test_a_unit_with_unfinished_work_can_still_dispatch_itself(scheduler, beads_project):
    only = bd_create(beads_project, "only", alloy_recipe="tdd-loop")
    _park_run(scheduler, beads_project, only, status=RUN_FAILED)

    assert scheduler.next_task() is not None
