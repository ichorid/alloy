"""`build_snapshot` and `alloy monitor --once --json`.

Assembles the frozen JSON shape from "Component 2" of
docs/plans/execution-monitor.md. These tests never call a real model; the
tdd-loop graph is driven for real through the scripted fake harness binaries
(as in tests/test_workflow.py), and `Engine` is constructed directly with a
stub `BeadsClient` so no real `bd` process is required except where a test
explicitly needs one (the CLI end-to-end tests use `beads_project`).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import (
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
from support import make_harness
from typer.testing import CliRunner

from alloy import beads as bd
from alloy.cli import app
from alloy.engine import Engine
from alloy.limits import window, write_cache
from alloy.models import DEFAULT_RECIPE_KEY, AgentResult
from alloy.monitor import build_snapshot
from alloy.paths import AlloyPaths
from alloy.scheduler import Scheduler
from alloy.store import RUN_CANCELLED, RUN_DONE, RUN_FAILED, RUN_RUNNING, Store

TOP_LEVEL_KEYS = {
    "root",
    "repo",
    "scheduler",
    "ready_count",
    "ready_capped_at",
    "lifetime",
    "runs",
    "limits",
    "session",
    "session_totals",
    "queue",
    "epics",
    "auxiliary_calls",
}
EPIC_ENTRY_KEYS = {
    "epic_id",
    "title",
    "total",
    "done",
    "done_ids",
    "running",
    "judge",
}
QUEUE_KEYS = {"ready", "ready_total", "blocked"}
READY_ENTRY_KEYS = {"bead_id", "title", "recipe", "priority", "complexity", "epic_id"}
BLOCKED_ENTRY_KEYS = {"bead_id", "title", "blocked_by", "epic_id"}
RUN_ENTRY_KEYS = {
    "bead_id",
    "run_id",
    "recipe",
    "status",
    "stage",
    "iteration",
    "max_iterations",
    "consiliums",
    "max_consiliums",
    "tests_summary",
    "checks",
    "elapsed_minutes",
    "current_calls",
    "tokens",
    "tokens_by_role",
    "judge",
    "worktree",
    "branch",
    "parent_run_id",
    "parent_bead_id",
    "complexity",
    "models_used",
    "epic_id",
    "title",
}
CURRENT_CALL_KEYS = {
    "role",
    "requested_runner",
    "effective_runner",
    "requested_model",
    "effective_model",
    "elapsed_seconds",
}
TOKENS_KEYS = {"input_tokens", "output_tokens", "total_tokens", "cost_usd"}
JUDGE_KEYS = {"raw", "effective", "matches_effective"}


class FakeBeads:
    """Stands in for `BeadsClient` in monitor snapshot tests."""

    def __init__(
        self,
        ready: tuple[str, ...] | list[bd.Bead] = (),
        *,
        blocked: list[bd.Bead] = (),
        memories: dict[str, str] | None = None,
        children: dict[str, list[bd.Bead]] | None = None,
        shows: dict[str, bd.Bead] | None = None,
        epics: dict[str, str | None] | None = None,
        epics_build_error: bool = False,
        beads_error: bool = False,
    ) -> None:
        if ready and isinstance(ready[0], bd.Bead):
            self._ready = list(ready)
        elif ready:
            self._ready = [
                bd.Bead(
                    id=bead_id,
                    title=bead_id,
                    metadata={bd.META_RECIPE: "tdd-loop"},
                )
                for bead_id in ready
            ]
        else:
            self._ready = []
        self._blocked = list(blocked)
        self._memories = dict(memories or {})
        self._children = dict(children or {})
        self._shows = dict(shows or {})
        self._epics = dict(epics or {})
        self._epics_build_error = epics_build_error
        self._beads_error = beads_error

    def _maybe_raise(self) -> None:
        if self._beads_error:
            raise bd.BeadsError("fake bd failure")

    def ready(
        self,
        *,
        recipe: str | None = None,
        limit: int = 50,
        include_unassigned: bool = False,
    ) -> list[bd.Bead]:
        self._maybe_raise()
        beads = sorted(self._ready, key=lambda bead: (bead.priority, bead.id))
        if recipe:
            beads = [bead for bead in beads if bead.recipe == recipe]
        elif not include_unassigned:
            beads = [bead for bead in beads if bead.recipe]
        return beads[:limit]

    def blocked(self) -> list[bd.Bead]:
        self._maybe_raise()
        return list(self._blocked)

    def memories(self) -> dict[str, str]:
        self._maybe_raise()
        return dict(self._memories)

    def children(self, parent_id: str) -> list[bd.Bead]:
        self._maybe_raise()
        if self._epics_build_error:
            raise bd.BeadsError("fake bd failure building epics")
        return list(self._children.get(parent_id, []))

    def show(self, bead_id: str) -> bd.Bead | None:
        self._maybe_raise()
        return self._shows.get(bead_id)

    def epic_for(self, bead_id: str, *, max_depth: int = 3) -> str | None:
        self._maybe_raise()
        return self._epics.get(bead_id)


def _engine(
    repo: Path,
    alloy_home: Path,
    *,
    beads: FakeBeads | None = None,
    ready_ids: tuple[str, ...] = (),
) -> Engine:
    paths = AlloyPaths.resolve(alloy_home).ensure()
    if beads is None:
        beads = FakeBeads(ready_ids)
    return Engine(repo=repo, paths=paths, store=Store(paths.alloy_db), beads=beads)


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


# -- header, with zero active runs -------------------------------------------


def test_zero_active_runs_still_has_a_fully_typed_header(project, alloy_home):
    engine = _engine(project, alloy_home, ready_ids=("t-1", "t-2", "t-3"))

    snapshot = build_snapshot(engine)

    assert snapshot["runs"] == []
    assert snapshot["root"] == str(engine.paths.root)
    assert snapshot["repo"] == str(engine.repo)
    assert isinstance(snapshot["ready_count"], int)
    assert snapshot["ready_count"] == 3
    assert snapshot["ready_capped_at"] == 1000
    assert isinstance(snapshot["lifetime"], dict)
    assert snapshot["lifetime"] == {"done": 0, "failed": 0, "cancelled": 0}
    assert isinstance(snapshot["scheduler"], dict)
    assert isinstance(snapshot["scheduler"]["running"], bool)
    assert snapshot["scheduler"]["running"] is False
    assert snapshot["scheduler"]["pid"] is None


def test_scheduler_running_reflects_a_live_pidfile(project, alloy_home):
    engine = _engine(project, alloy_home)
    engine.paths.scheduler_pid.write_text(str(os.getpid()), encoding="utf-8")

    snapshot = build_snapshot(engine)

    assert snapshot["scheduler"] == {"running": True, "pid": os.getpid()}


def test_lifetime_counts_every_terminal_status_from_run_status_totals(project, alloy_home):
    engine = _engine(project, alloy_home)
    engine.store.create_run(
        run_id="r-done",
        bead_id="t-1",
        thread_id="r-done",
        recipe="tdd-loop",
        repo=project,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    engine.store.finish_run("r-done", status="done", outcome="done")
    engine.store.create_run(
        run_id="r-failed",
        bead_id="t-2",
        thread_id="r-failed",
        recipe="tdd-loop",
        repo=project,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    engine.store.finish_run("r-failed", status="failed", outcome="failed")

    snapshot = build_snapshot(engine)

    assert snapshot["lifetime"] == {"done": 1, "failed": 1, "cancelled": 0}


def test_freshly_initialized_alloy_home_does_not_raise(project, alloy_home):
    engine = _engine(project, alloy_home)

    snapshot = build_snapshot(engine)

    assert snapshot["runs"] == []


# -- run entry title falls back to a description snippet --------------------


def _run_with_bead(engine: Engine, run_id: str, bead_id: str) -> None:
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
    engine.store.update_run(run_id, status=RUN_RUNNING, pid=os.getpid())


def test_run_entry_title_uses_bead_title_when_present(project, alloy_home):
    bead_id = "bead-run-titled"
    beads = FakeBeads(
        shows={
            bead_id: bd.Bead(id=bead_id, title="Fix the thing", description="Longer story."),
        }
    )
    engine = _engine(project, alloy_home, beads=beads)
    _run_with_bead(engine, "run-titled", bead_id)

    run = build_snapshot(engine)["runs"][0]

    assert run["title"] == "Fix the thing"


def test_run_entry_title_falls_back_to_description_snippet_when_title_is_blank(
    project,
    alloy_home,
):
    bead_id = "bead-run-untitled"
    beads = FakeBeads(
        shows={
            bead_id: bd.Bead(
                id=bead_id,
                title="",
                description="Fix the flaky retry loop.\nMore detail.",
            ),
        }
    )
    engine = _engine(project, alloy_home, beads=beads)
    _run_with_bead(engine, "run-untitled", bead_id)

    run = build_snapshot(engine)["runs"][0]

    assert run["title"] == "Fix the flaky retry loop."


def test_run_entry_title_is_none_when_bead_has_no_title_or_description(project, alloy_home):
    bead_id = "bead-run-empty"
    beads = FakeBeads(shows={bead_id: bd.Bead(id=bead_id, title="", description="")})
    engine = _engine(project, alloy_home, beads=beads)
    _run_with_bead(engine, "run-empty", bead_id)

    run = build_snapshot(engine)["runs"][0]

    assert run["title"] is None


# -- shape of a run entry ----------------------------------------------------


async def test_full_key_set_at_top_level_and_in_one_run_entry(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script())
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    engine = _engine(project, alloy_home)
    snapshot = build_snapshot(engine)

    assert set(snapshot.keys()) == TOP_LEVEL_KEYS
    assert len(snapshot["runs"]) == 1
    run = snapshot["runs"][0]
    assert set(run.keys()) == RUN_ENTRY_KEYS
    assert run["run_id"] == harness.run_id
    assert run["bead_id"] == harness.bead.id
    assert run["recipe"] == harness.recipe_config.name

    for call in run["current_calls"]:
        assert set(call.keys()) == CURRENT_CALL_KEYS
    assert set(run["tokens"].keys()) == TOKENS_KEYS
    for role_tokens in run["tokens_by_role"].values():
        assert set(role_tokens.keys()) == TOKENS_KEYS
    if run["judge"] is not None:
        assert set(run["judge"].keys()) == JUDGE_KEYS
        assert set(run["judge"]["raw"].keys()) == {"decision", "confidence"}
        assert set(run["judge"]["effective"].keys()) == {"decision", "reason"}


# -- null / empty rules -------------------------------------------------------


def test_run_with_no_checks_has_null_checks_in_snapshot(project, alloy_home):
    engine = _engine(project, alloy_home)
    make_harness(project, alloy_home)  # creates the run row; never started

    snapshot = build_snapshot(engine)

    assert len(snapshot["runs"]) == 1
    assert snapshot["runs"][0]["checks"] is None


async def test_finished_run_checks_object_in_snapshot(project, alloy_home, fake_harnesses):
    import sys

    fake_harnesses.configure(
        script(
            verifier=[
                verifier_run_entry(f"{sys.executable} -m pytest -q", kind="regression"),
                verifier_stop_entry("suite green"),
            ],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    engine = _engine(project, alloy_home)
    snapshot = build_snapshot(engine)

    run = snapshot["runs"][0]
    assert isinstance(run["checks"], dict)
    assert run["checks"]["total"] > 0


def test_run_with_no_checkpoint_yet_has_null_judge_and_empty_current_calls(project, alloy_home):
    engine = _engine(project, alloy_home)
    harness = make_harness(project, alloy_home)  # creates the run row; never started

    snapshot = build_snapshot(engine)

    assert len(snapshot["runs"]) == 1
    run = snapshot["runs"][0]
    assert run["run_id"] == harness.run_id
    assert run["current_calls"] == []
    assert run["judge"] is None
    assert run["tokens"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": None,
    }
    assert run["tokens_by_role"] == {}
    assert run["consiliums"] == 0
    assert run["max_consiliums"] == harness.recipe_config.limits.max_consiliums
    assert run["max_iterations"] == harness.recipe_config.limits.max_iterations


def test_run_with_an_in_flight_call_reports_requested_vs_effective(project, alloy_home):
    engine = _engine(project, alloy_home)
    harness = make_harness(project, alloy_home)
    engine.store.start_call(
        "call-1",
        run_id=harness.run_id,
        bead_id=harness.bead.id,
        role="implement",
        runner="codex",
        model=None,
    )

    snapshot = build_snapshot(engine)

    run = snapshot["runs"][0]
    assert len(run["current_calls"]) == 1
    call = run["current_calls"][0]
    assert call["role"] == "implement"
    # tdd-loop.yaml configures `implement: {runner: astra}`; the ledger recorded
    # what actually ran (the astra -> codex alias), so requested and effective differ.
    assert call["requested_runner"] == "astra"
    assert call["effective_runner"] == "codex"
    assert isinstance(call["elapsed_seconds"], (int, float))
    assert call["elapsed_seconds"] >= 0


def test_repository_memory_review_call_is_visible_without_a_bead_run(project, alloy_home):
    engine = _engine(project, alloy_home)
    engine.store.start_call(
        "memory-call",
        run_id="memory-review-test",
        bead_id="memory-review",
        role="memory_reviewer",
        runner="codex",
        model="gpt-6-sol",
    )
    snapshot = build_snapshot(engine)
    assert snapshot["auxiliary_calls"][0]["effective_model"] == "gpt-6-sol"
    assert snapshot["auxiliary_calls"][0]["role"] == "memory_reviewer"


async def test_judge_mismatch_when_the_guard_overrides_a_done_decision(project, alloy_home, fake_harnesses):
    """Judge always says 'retry' on a green suite: once the iteration limit is
    breached the guard overrides it to 'human' -- raw and effective must
    disagree in the final state, and `matches_effective` must say so. (A red
    required check never reaches the judge any more; it goes straight to
    repair, so the override can only come from a limit.)"""
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=True)],
            judge=[judge_entry("retry", "one more pass")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    engine = _engine(project, alloy_home)
    snapshot = build_snapshot(engine)
    run = snapshot["runs"][0]

    assert run["judge"] is not None
    assert run["judge"]["raw"]["decision"] == "retry"
    assert run["judge"]["effective"]["decision"] != "retry"
    assert run["judge"]["matches_effective"] is False


# -- a bead with two recorded runs -------------------------------------------


async def test_two_runs_for_the_same_bead_each_show_their_own_state(project, alloy_home, fake_harnesses):
    store = Store(alloy_home / "alloy.db")

    # Both runs share the bead's worktree. The parked run must not leave a
    # working slugify behind, or the second run's targeted baseline is green
    # and prove_red parks it at the human gate instead of letting it finish.
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False)],
            judge=[judge_entry("human", "need a decision")],
        )
    )
    parked = make_harness(project, alloy_home, store=store, run_id="run-parked")
    try:
        await parked.start()
    finally:
        parked.close()

    fake_harnesses.reset_calls()
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[judge_entry("retry"), judge_entry("done")],
        )
    )
    finished = make_harness(project, alloy_home, store=store, run_id="run-finished")
    try:
        await finished.start()
    finally:
        finished.close()

    engine = _engine(project, alloy_home)
    snapshot = build_snapshot(engine)

    assert len(snapshot["runs"]) == 2
    by_id = {row["run_id"]: row for row in snapshot["runs"]}
    assert set(by_id) == {"run-parked", "run-finished"}

    assert by_id["run-parked"]["stage"] == "guard"
    assert by_id["run-parked"]["judge"]["effective"]["decision"] == "human"
    assert by_id["run-parked"]["iteration"] == 1

    assert by_id["run-finished"]["stage"] == "finished"
    assert by_id["run-finished"]["judge"]["effective"]["decision"] == "done"
    assert by_id["run-finished"]["iteration"] == 2


# -- CLI: `alloy monitor --once --json` --------------------------------------


def test_cli_monitor_once_json_exits_zero_and_matches_build_snapshot(beads_project, alloy_home):
    engine = Engine.open(beads_project, alloy_home)
    expected = build_snapshot(engine)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "monitor",
            "--once",
            "--json",
            "--repo",
            str(beads_project),
            "--root",
            str(alloy_home),
        ],
    )

    assert result.exit_code == 0
    import json

    payload = json.loads(result.stdout)
    assert payload == expected
    assert payload["runs"] == []
    assert isinstance(payload["ready_count"], int)
    assert isinstance(payload["lifetime"], dict)
    assert set(payload["lifetime"].keys()) == {"done", "failed", "cancelled"}
    assert isinstance(payload["scheduler"]["running"], bool)


def test_snapshot_child_run_carries_parent_run_id_and_parent_complexity(project, alloy_home):
    """Active parent/child rows are seeded directly; finished children drop off the monitor."""
    import os

    from alloy.store import RUN_RUNNING

    engine = _engine(project, alloy_home)
    parent_run_id = "run-parent"
    child_run_id = "run-child"
    engine.store.create_run(
        run_id=parent_run_id,
        bead_id="alloy-parent",
        thread_id=parent_run_id,
        recipe="tdd-loop",
        repo=project,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    engine.store.create_run(
        run_id=child_run_id,
        bead_id="alloy-child",
        thread_id=child_run_id,
        recipe="tdd-loop",
        repo=project,
        worktree=None,
        branch=None,
        log_dir=None,
        parent_run_id=parent_run_id,
    )
    engine.store.update_run(parent_run_id, status=RUN_RUNNING, pid=os.getpid(), complexity="simple")
    engine.store.update_run(child_run_id, status=RUN_RUNNING, pid=os.getpid())

    snapshot = build_snapshot(engine)
    by_id = {row["run_id"]: row for row in snapshot["runs"]}

    assert set(by_id) == {parent_run_id, child_run_id}
    assert by_id[child_run_id]["parent_run_id"] == parent_run_id
    assert by_id[parent_run_id]["complexity"] == "simple"


def test_snapshot_child_run_resolves_parent_bead_id(project, alloy_home):
    """Acceptance: child run entry carries parent_bead_id; parent entry has null."""
    import os

    from alloy.store import RUN_RUNNING

    engine = _engine(project, alloy_home)
    parent_run_id = "run-parent"
    child_run_id = "run-child"
    engine.store.create_run(
        run_id=parent_run_id,
        bead_id="alloy-parent",
        thread_id=parent_run_id,
        recipe="tdd-loop",
        repo=project,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    engine.store.create_run(
        run_id=child_run_id,
        bead_id="alloy-child",
        thread_id=child_run_id,
        recipe="tdd-loop",
        repo=project,
        worktree=None,
        branch=None,
        log_dir=None,
        parent_run_id=parent_run_id,
    )
    engine.store.update_run(parent_run_id, status=RUN_RUNNING, pid=os.getpid())
    engine.store.update_run(child_run_id, status=RUN_RUNNING, pid=os.getpid())

    snapshot = build_snapshot(engine)
    by_id = {row["run_id"]: row for row in snapshot["runs"]}

    assert by_id[child_run_id]["parent_bead_id"] == "alloy-parent"
    assert by_id[parent_run_id]["parent_bead_id"] is None


def test_cli_monitor_once_json_with_an_active_run(beads_project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script())
    bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    store = Store(alloy_home / "alloy.db")
    make_harness(beads_project, alloy_home, store=store)  # creates the run row only

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "monitor",
            "--once",
            "--json",
            "--repo",
            str(beads_project),
            "--root",
            str(alloy_home),
        ],
    )

    assert result.exit_code == 0
    import json

    payload = json.loads(result.stdout)
    assert len(payload["runs"]) == 1
    assert payload["runs"][0]["current_calls"] == []
    assert payload["runs"][0]["judge"] is None


# -- monitor limits: cache + models_used (alloy-w9d.8) -----------------------


def _cached_claude_limits() -> dict:
    return {
        "harness": "claude",
        "installed": True,
        "available": True,
        "fetched_at": "2026-09-23T10:00:00+00:00",
        "as_of": "2026-09-23T10:00:00+00:00",
        "source": "oauth-usage-api",
        "error": None,
        "status": None,
        "windows": [
            window("five_hour", "5h", 42.0, None),
            window("seven_day", "weekly", 61.0, None),
            window("seven_day_opus", "weekly opus", 80.0, None, model="opus"),
            window("seven_day_fable", "weekly fable", 12.0, None, model="fable"),
        ],
    }


def _cached_codex_limits() -> dict:
    return {
        "harness": "codex",
        "installed": True,
        "available": True,
        "fetched_at": "2026-09-23T10:00:00+00:00",
        "as_of": "2026-09-23T10:00:00+00:00",
        "source": "session-rollout",
        "error": None,
        "status": None,
        "windows": [
            window("primary", "5h", 53.0, None),
            window("secondary", "weekly", 51.0, None),
        ],
    }


def _iso(year: int, month: int, day: int, hour: int, minute: int, second: int) -> str:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).isoformat()


def _finish_agent_call(
    store: Store,
    run_id: str,
    call_id: str,
    *,
    role: str = "implement",
    runner: str,
    model: str | None,
    started_at: datetime,
) -> None:
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO inflight_calls (call_id, run_id, bead_id, role, runner, model, started_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                call_id,
                run_id,
                f"bead-{run_id}",
                role,
                runner,
                model,
                started_at.isoformat(),
            ),
        )
    store.finish_call(
        call_id,
        run_id=run_id,
        bead_id=f"bead-{run_id}",
        role=role,
        iteration=0,
        result=AgentResult(
            runner=runner,
            model=model,
            ok=True,
            exit_code=0,
            text="done",
            structured=None,
            started_at=started_at,
            ended_at=started_at,
            duration_s=1.0,
            usage={"input_tokens": 10, "output_tokens": 1},
            log_path="/tmp/log",
            prompt_hash="deadbeef",
        ),
    )


def _active_run(engine: Engine, run_id: str = "run-limits") -> None:
    engine.store.create_run(
        run_id=run_id,
        bead_id=f"bead-{run_id}",
        thread_id=run_id,
        recipe="tdd-loop",
        repo=engine.repo,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    engine.store.update_run(run_id, status=RUN_RUNNING, pid=os.getpid())


def test_snapshot_limits_installed_harnesses_only_with_cache_miss_as_not_probed(
    project,
    alloy_home,
    fake_harnesses,
):
    """Acceptance: installed claude+codex only; cache has claude; codex is 'not probed yet'."""
    fake_harnesses.remove("cursor-agent")
    paths = AlloyPaths.resolve(alloy_home).ensure()
    write_cache(paths, {"claude": _cached_claude_limits()})

    engine = _engine(project, alloy_home)
    snapshot = build_snapshot(engine)

    assert set(snapshot["limits"].keys()) == {"claude", "codex"}
    assert snapshot["limits"]["claude"]["available"] is True
    assert snapshot["limits"]["codex"]["available"] is False
    assert snapshot["limits"]["codex"]["error"] == "not probed yet"
    assert "cursor" not in snapshot["limits"]


def test_snapshot_models_used_joins_harness_windows_by_model_family(
    project,
    alloy_home,
    fake_harnesses,
):
    """Acceptance: fable gets account-wide + fable windows; codex gets both windows."""
    fake_harnesses.remove("cursor-agent")
    paths = AlloyPaths.resolve(alloy_home).ensure()
    write_cache(
        paths,
        {
            "claude": _cached_claude_limits(),
            "codex": _cached_codex_limits(),
        },
    )

    engine = _engine(project, alloy_home)
    _active_run(engine)
    _finish_agent_call(
        engine.store,
        "run-limits",
        "call-fable",
        runner="claude-write",
        model="fable",
        started_at=datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc),
    )
    _finish_agent_call(
        engine.store,
        "run-limits",
        "call-luna",
        runner="codex",
        model="gpt-5.6-luna",
        started_at=datetime(2026, 9, 22, 9, 0, 0, tzinfo=timezone.utc),
    )

    snapshot = build_snapshot(engine)
    run = snapshot["runs"][0]

    assert len(run["models_used"]) == 2
    assert run["models_used"][0]["runner"] == "claude-write"
    assert run["models_used"][0]["model"] == "fable"
    assert list(run["models_used"][0]["windows"].keys()) == [
        "five_hour",
        "seven_day",
        "seven_day_fable",
    ]
    assert run["models_used"][1]["runner"] == "codex"
    assert run["models_used"][1]["model"] == "gpt-5.6-luna"
    assert list(run["models_used"][1]["windows"].keys()) == ["primary", "secondary"]


def test_snapshot_always_has_limits_and_run_models_used(
    project,
    alloy_home,
    fake_harnesses,
):
    """Acceptance: limits and models_used are always present; empty when absent."""
    for name in ("claude", "codex", "cursor-agent"):
        fake_harnesses.remove(name)

    engine = _engine(project, alloy_home)
    _active_run(engine)

    snapshot = build_snapshot(engine)

    assert "limits" in snapshot
    assert snapshot["limits"] == {}
    assert len(snapshot["runs"]) == 1
    assert snapshot["runs"][0]["models_used"] == []


def test_build_snapshot_never_probes_limits(
    project,
    alloy_home,
    fake_harnesses,
    monkeypatch: pytest.MonkeyPatch,
):
    """Acceptance: build_snapshot reads cache only; harness fetchers must not run."""
    fake_harnesses.remove("cursor-agent")
    paths = AlloyPaths.resolve(alloy_home).ensure()
    write_cache(paths, {"claude": _cached_claude_limits()})

    def _raise(*_args, **_kwargs):
        raise AssertionError("build_snapshot must not probe harness limits")

    monkeypatch.setattr("alloy.limits.claude.default_fetch", _raise)
    monkeypatch.setattr("alloy.limits.codex.probe", _raise)
    monkeypatch.setattr("alloy.limits.cursor.default_fetch", _raise)
    monkeypatch.setattr("alloy.limits.probe_all", _raise)

    engine = _engine(project, alloy_home)
    snapshot = build_snapshot(engine)

    assert set(snapshot["limits"].keys()) == {"claude", "codex"}


# -- monitor session: finished runs + session_totals (alloy-w9d.9) -----------


def _write_scheduler_session(
    paths: AlloyPaths,
    *,
    started_at: str,
    pid: int,
    ended_at: str | None = None,
) -> None:
    payload = {"pid": pid, "started_at": started_at, "ended_at": ended_at}
    paths.scheduler_session.write_text(json.dumps(payload), encoding="utf-8")


def _finish_terminal_run(
    store: Store,
    run_id: str,
    *,
    status: str,
    ended_at: str,
    repo: Path,
) -> None:
    store.create_run(
        run_id=run_id,
        bead_id=f"bead-{run_id}",
        thread_id=run_id,
        recipe="tdd-loop",
        repo=repo,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    store.update_run(run_id, status=status, ended_at=ended_at, outcome=status)


def test_snapshot_session_appends_finished_runs_and_session_totals(project, alloy_home):
    """Acceptance: active first, then done/failed since session start; pre-session cancelled omitted."""
    session_start = _iso(2026, 9, 23, 10, 0, 0)
    engine = _engine(project, alloy_home)
    _write_scheduler_session(
        engine.paths,
        started_at=session_start,
        pid=os.getpid(),
        ended_at=None,
    )
    _active_run(engine, "run-active")
    _finish_terminal_run(
        engine.store,
        "run-done",
        status=RUN_DONE,
        ended_at=_iso(2026, 9, 23, 10, 1, 0),
        repo=project,
    )
    _finish_terminal_run(
        engine.store,
        "run-failed",
        status=RUN_FAILED,
        ended_at=_iso(2026, 9, 23, 10, 2, 0),
        repo=project,
    )
    _finish_terminal_run(
        engine.store,
        "run-cancelled",
        status=RUN_CANCELLED,
        ended_at=_iso(2026, 9, 23, 9, 59, 0),
        repo=project,
    )

    snapshot = build_snapshot(engine)

    assert snapshot["session"] == {
        "started_at": session_start,
        "ended_at": None,
        "pid": os.getpid(),
    }
    assert snapshot["session_totals"] == {"done": 1, "failed": 1, "cancelled": 0}
    assert [row["run_id"] for row in snapshot["runs"]] == [
        "run-active",
        "run-done",
        "run-failed",
    ]


def test_snapshot_without_scheduler_session_file_lists_only_active_runs(project, alloy_home):
    """Acceptance: no scheduler.json -> null session, zero session_totals, active runs only."""
    engine = _engine(project, alloy_home)
    _active_run(engine, "run-active")
    _finish_terminal_run(
        engine.store,
        "run-done",
        status=RUN_DONE,
        ended_at=_iso(2026, 9, 23, 10, 1, 0),
        repo=project,
    )

    snapshot = build_snapshot(engine)

    assert snapshot["session"] == {"started_at": None, "ended_at": None, "pid": None}
    assert snapshot["session_totals"] == {"done": 0, "failed": 0, "cancelled": 0}
    assert [row["run_id"] for row in snapshot["runs"]] == ["run-active"]


def test_snapshot_finished_run_entry_has_full_shape_and_empty_current_calls(project, alloy_home):
    """Acceptance: appended finished runs use _run_entry with empty current_calls and terminal status."""
    session_start = _iso(2026, 9, 23, 10, 0, 0)
    engine = _engine(project, alloy_home)
    _write_scheduler_session(
        engine.paths,
        started_at=session_start,
        pid=os.getpid(),
        ended_at=None,
    )
    _finish_terminal_run(
        engine.store,
        "run-done",
        status=RUN_DONE,
        ended_at=_iso(2026, 9, 23, 10, 1, 0),
        repo=project,
    )

    snapshot = build_snapshot(engine)

    assert len(snapshot["runs"]) == 1
    run = snapshot["runs"][0]
    assert set(run.keys()) == RUN_ENTRY_KEYS
    assert run["current_calls"] == []
    assert run["status"] == RUN_DONE


# -- monitor queue: ready order, blocked, truncation, error fallback (alloy-byo.2) --


def _dispatchable_bead_ids(engine: Engine) -> list[str]:
    """Mirror Scheduler.next_task filtering over the full ready list."""
    scheduler = Scheduler(engine=engine)
    from alloy import recipes

    known = set(recipes.names())
    default = engine.beads.memories().get(DEFAULT_RECIPE_KEY) if scheduler.recipe_filter is None else None
    if default and default not in known:
        default = None
    default_recipe = default or None
    ids: list[str] = []
    for bead in engine.beads.ready(
        recipe=scheduler.recipe_filter,
        include_unassigned=default_recipe is not None,
        limit=10_000,
    ):
        if (bead.recipe or default_recipe) in known:
            ids.append(bead.id)
    return ids


def _queue_bead(
    bead_id: str,
    *,
    title: str,
    priority: int = 2,
    recipe: str | None = "tdd-loop",
    complexity: str | None = None,
) -> bd.Bead:
    metadata: dict[str, str] = {}
    if recipe:
        metadata[bd.META_RECIPE] = recipe
    if complexity:
        metadata[bd.META_COMPLEXITY] = complexity
    return bd.Bead(id=bead_id, title=title, priority=priority, metadata=metadata)


def test_snapshot_queue_ready_matches_scheduler_dispatch_order(project, alloy_home):
    """Acceptance: queue.ready bead ids follow Scheduler.next_task dispatch order."""
    beads = FakeBeads(
        [
            _queue_bead("alloy-z.3", title="third", priority=2, recipe="tdd-loop"),
            _queue_bead("alloy-z.1", title="first", priority=0, recipe="tdd-loop"),
            _queue_bead("alloy-z.2", title="second", priority=1, recipe="tdd-loop"),
            _queue_bead("alloy-z.4", title="unknown recipe", priority=0, recipe="no-such"),
            _queue_bead("alloy-z.5", title="needs default", priority=1),
        ],
        memories={DEFAULT_RECIPE_KEY: "tdd-loop"},
        epics={
            "alloy-z.1": "alloy-epic",
            "alloy-z.2": None,
        },
    )
    engine = _engine(project, alloy_home, beads=beads)

    snapshot = build_snapshot(engine)
    queue = snapshot["queue"]

    assert set(queue.keys()) == QUEUE_KEYS
    expected_ids = _dispatchable_bead_ids(engine)
    assert [entry["bead_id"] for entry in queue["ready"]] == expected_ids
    assert queue["ready_total"] == len(expected_ids)
    assert expected_ids == ["alloy-z.1", "alloy-z.2", "alloy-z.5", "alloy-z.3"]

    first = queue["ready"][0]
    assert set(first.keys()) == READY_ENTRY_KEYS
    assert first == {
        "bead_id": "alloy-z.1",
        "title": "first",
        "recipe": "tdd-loop",
        "priority": 0,
        "complexity": None,
        "epic_id": "alloy-epic",
    }
    assert queue["ready"][1]["recipe"] == "tdd-loop"
    assert queue["ready"][2]["recipe"] == "tdd-loop"


def test_snapshot_queue_ready_truncates_at_fifty_and_reports_ready_total(project, alloy_home):
    """Acceptance: 60 dispatchable ready beads -> 50 listed, ready_total == 60."""
    ready = [_queue_bead(f"alloy-q.{index:02d}", title=f"task {index}", priority=2) for index in range(60)]
    engine = _engine(project, alloy_home, beads=FakeBeads(ready))

    snapshot = build_snapshot(engine)
    queue = snapshot["queue"]

    assert len(queue["ready"]) == 50
    assert queue["ready_total"] == 60
    assert [entry["bead_id"] for entry in queue["ready"]] == _dispatchable_bead_ids(engine)[:50]


def test_snapshot_queue_blocked_includes_blocked_by_and_epic_id(project, alloy_home):
    """Acceptance: blocked[0].blocked_by matches the fake bd blockers."""
    blocked = bd.Bead(
        id="alloy-blocked.1",
        title="waiting on deps",
        blocked_by=["alloy-a", "alloy-b"],
    )
    engine = _engine(
        project,
        alloy_home,
        beads=FakeBeads(blocked=[blocked], epics={"alloy-blocked.1": "alloy-epic"}),
    )

    snapshot = build_snapshot(engine)
    queue = snapshot["queue"]

    assert queue["ready"] == []
    assert queue["ready_total"] == 0
    assert len(queue["blocked"]) == 1
    entry = queue["blocked"][0]
    assert set(entry.keys()) == BLOCKED_ENTRY_KEYS
    assert entry["bead_id"] == "alloy-blocked.1"
    assert entry["title"] == "waiting on deps"
    assert entry["blocked_by"] == ["alloy-a", "alloy-b"]
    assert entry["epic_id"] == "alloy-epic"


def test_snapshot_queue_error_fallback_still_returns_runs(project, alloy_home):
    """Acceptance: BeadsError -> empty queue shape; active runs are still present."""
    engine = _engine(project, alloy_home, beads=FakeBeads(beads_error=True))
    _active_run(engine, "run-active")

    snapshot = build_snapshot(engine)

    assert snapshot["queue"] == {"ready": [], "ready_total": 0, "blocked": []}
    assert len(snapshot["runs"]) == 1
    assert snapshot["runs"][0]["run_id"] == "run-active"


# -- snapshot epics[] and run epic_id (alloy-byo.3) ---------------------------


_EPIC_ID = "E"
_EPIC_TITLE = "Monitor TUI redesign"
_DONE_CHILDREN = ("E.1", "E.2", "E.3", "E.4")
_OPEN_CHILDREN = ("E.5", "E.6", "E.7", "E.8", "E.9")


def _epic_children() -> list[bd.Bead]:
    return [
        *[bd.Bead(id=child_id, title=child_id, status=bd.STATUS_DONE) for child_id in _DONE_CHILDREN],
        *[bd.Bead(id=child_id, title=child_id, status="open") for child_id in _OPEN_CHILDREN],
    ]


def _seed_active_run(
    engine: Engine,
    run_id: str,
    bead_id: str,
    *,
    stage: str,
) -> None:
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
    engine.store.update_run(run_id, status=RUN_RUNNING, pid=os.getpid(), stage=stage)


def _epic_progress_beads() -> FakeBeads:
    epic_children = _epic_children()
    return FakeBeads(
        children={_EPIC_ID: epic_children},
        shows={_EPIC_ID: bd.Bead(id=_EPIC_ID, title=_EPIC_TITLE, issue_type="epic")},
        epics={
            "E.5": _EPIC_ID,
            "E.6": _EPIC_ID,
            "orphan": None,
        },
    )


def test_snapshot_epics_progress_from_children_and_active_runs(project, alloy_home):
    """Acceptance: epic children progress plus active run/judge counts; runs carry epic_id."""
    engine = _engine(project, alloy_home, beads=_epic_progress_beads())
    _seed_active_run(engine, "run-judge", "E.5", stage="judge")
    _seed_active_run(engine, "run-active", "E.6", stage="tests")
    _seed_active_run(engine, "run-orphan", "orphan", stage="implement")

    snapshot = build_snapshot(engine)

    assert "epics" in snapshot
    assert len(snapshot["epics"]) == 1
    epic = snapshot["epics"][0]
    assert set(epic.keys()) == EPIC_ENTRY_KEYS
    assert epic == {
        "epic_id": _EPIC_ID,
        "title": _EPIC_TITLE,
        "total": 9,
        "done": 4,
        "done_ids": list(_DONE_CHILDREN),
        "running": 2,
        "judge": 1,
    }

    by_id = {row["run_id"]: row for row in snapshot["runs"]}
    assert by_id["run-judge"]["epic_id"] == _EPIC_ID
    assert by_id["run-active"]["epic_id"] == _EPIC_ID
    assert by_id["run-orphan"]["epic_id"] is None


def test_snapshot_epics_empty_on_beads_error(project, alloy_home):
    """Acceptance: bd failure while building epics yields [] but runs still return."""
    beads = _epic_progress_beads()
    beads._epics_build_error = True
    engine = _engine(project, alloy_home, beads=beads)
    _seed_active_run(engine, "run-judge", "E.5", stage="judge")

    snapshot = build_snapshot(engine)

    assert snapshot["epics"] == []
    assert len(snapshot["runs"]) == 1
