"""`build_snapshot` and `alloy monitor --once --json`.

Assembles the frozen JSON shape from "Component 2" of
docs/plans/execution-monitor.md. These tests never call a real model; the
tdd-loop graph is driven for real through the scripted fake harness binaries
(as in tests/test_workflow.py), and `Engine` is constructed directly with a
stub `BeadsClient` so no real `bd` process is required except where a test
explicitly needs one (the CLI end-to-end tests use `beads_project`).
"""

from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner

from alloy.cli import app
from alloy.engine import Engine
from alloy.monitor import build_snapshot
from alloy.paths import AlloyPaths
from alloy.store import Store
from conftest import (
    bd_create,
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    write_tests_entry,
)
from support import make_harness

TOP_LEVEL_KEYS = {
    "root", "repo", "scheduler", "ready_count", "ready_capped_at", "lifetime", "runs",
}
RUN_ENTRY_KEYS = {
    "bead_id", "run_id", "recipe", "status", "stage", "iteration", "max_iterations",
    "consiliums", "max_consiliums", "tests_summary", "elapsed_minutes", "current_calls",
    "tokens", "tokens_by_role", "judge", "worktree", "branch",
    "parent_run_id", "complexity",
}
CURRENT_CALL_KEYS = {
    "role", "requested_runner", "effective_runner", "requested_model",
    "effective_model", "elapsed_seconds",
}
TOKENS_KEYS = {"input_tokens", "output_tokens", "total_tokens", "cost_usd"}
JUDGE_KEYS = {"raw", "effective", "matches_effective"}


class FakeBeads:
    """Stands in for `BeadsClient`; `build_snapshot` only ever calls `ready()`."""

    def __init__(self, ready_ids: tuple[str, ...] = ()) -> None:
        self._ready = list(ready_ids)

    def ready(self, *, recipe: str | None = None, limit: int = 50):
        return self._ready[:limit]


def _engine(repo: Path, alloy_home: Path, *, ready_ids: tuple[str, ...] = ()) -> Engine:
    paths = AlloyPaths.resolve(alloy_home).ensure()
    return Engine(repo=repo, paths=paths, store=Store(paths.alloy_db), beads=FakeBeads(ready_ids))


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
        run_id="r-done", bead_id="t-1", thread_id="r-done", recipe="tdd-loop", repo=project,
        worktree=None, branch=None, log_dir=None,
    )
    engine.store.finish_run("r-done", status="done", outcome="done")
    engine.store.create_run(
        run_id="r-failed", bead_id="t-2", thread_id="r-failed", recipe="tdd-loop", repo=project,
        worktree=None, branch=None, log_dir=None,
    )
    engine.store.finish_run("r-failed", status="failed", outcome="failed")

    snapshot = build_snapshot(engine)

    assert snapshot["lifetime"] == {"done": 1, "failed": 1, "cancelled": 0}


def test_freshly_initialized_alloy_home_does_not_raise(project, alloy_home):
    engine = _engine(project, alloy_home)

    snapshot = build_snapshot(engine)

    assert snapshot["runs"] == []


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
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": None,
    }
    assert run["tokens_by_role"] == {}
    assert run["consiliums"] == 0
    assert run["max_consiliums"] == harness.recipe_config.limits.max_consiliums
    assert run["max_iterations"] == harness.recipe_config.limits.max_iterations


def test_run_with_an_in_flight_call_reports_requested_vs_effective(project, alloy_home):
    engine = _engine(project, alloy_home)
    harness = make_harness(project, alloy_home)
    engine.store.start_call(
        "call-1", run_id=harness.run_id, bead_id=harness.bead.id, role="implement",
        runner="codex", model=None,
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


async def test_judge_mismatch_when_the_guard_overrides_a_done_decision(
    project, alloy_home, fake_harnesses
):
    """Judge always says 'done' while tests keep failing: the guard overrides
    every time (first to 'retry', then to 'human' once the iteration limit is
    breached) -- raw and effective must disagree in the final state, and
    `matches_effective` must say so."""
    fake_harnesses.configure(
        script(implement=[implement_entry(succeed=False)],
               judge=[judge_entry("done", "looks complete")])
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
    assert run["judge"]["raw"]["decision"] == "done"
    assert run["judge"]["effective"]["decision"] != "done"
    assert run["judge"]["matches_effective"] is False


# -- a bead with two recorded runs -------------------------------------------


async def test_two_runs_for_the_same_bead_each_show_their_own_state(
    project, alloy_home, fake_harnesses
):
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
        script(implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
               judge=[judge_entry("retry"), judge_entry("done")])
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
        ["monitor", "--once", "--json", "--repo", str(beads_project), "--root", str(alloy_home)],
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
        run_id=parent_run_id, bead_id="alloy-parent", thread_id=parent_run_id,
        recipe="tdd-loop", repo=project, worktree=None, branch=None, log_dir=None,
    )
    engine.store.create_run(
        run_id=child_run_id, bead_id="alloy-child", thread_id=child_run_id,
        recipe="tdd-loop", repo=project, worktree=None, branch=None, log_dir=None,
        parent_run_id=parent_run_id,
    )
    engine.store.update_run(parent_run_id, status=RUN_RUNNING, pid=os.getpid(), complexity="simple")
    engine.store.update_run(child_run_id, status=RUN_RUNNING, pid=os.getpid())

    snapshot = build_snapshot(engine)
    by_id = {row["run_id"]: row for row in snapshot["runs"]}

    assert set(by_id) == {parent_run_id, child_run_id}
    assert by_id[child_run_id]["parent_run_id"] == parent_run_id
    assert by_id[parent_run_id]["complexity"] == "simple"


def test_cli_monitor_once_json_with_an_active_run(beads_project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    store = Store(alloy_home / "alloy.db")
    make_harness(beads_project, alloy_home, store=store)  # creates the run row only

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["monitor", "--once", "--json", "--repo", str(beads_project), "--root", str(alloy_home)],
    )

    assert result.exit_code == 0
    import json

    payload = json.loads(result.stdout)
    assert len(payload["runs"]) == 1
    assert payload["runs"][0]["current_calls"] == []
    assert payload["runs"][0]["judge"] is None
