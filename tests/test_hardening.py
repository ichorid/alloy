"""Regressions for the defects recorded in docs/journal.md."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from alloy import beads as bd
from alloy.config import RoleSpec
from alloy.engine import Engine, EngineError
from alloy.procs import pid_alive
from alloy.runners import RunnerRegistry
from alloy.store import RUN_RUNNING
from alloy.verify import parse_counts, run_tests, command_env
from conftest import (
    bd_create,
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    write_tests_entry,
)
from support import load_config, make_harness


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


# -- journal 13: pytest summary parsing ---------------------------------------


def test_collection_errors_are_not_double_counted():
    output = (
        "ERROR tests/test_usage.py\n"
        "!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!\n"
        "1 error in 0.24s\n"
    )
    assert parse_counts(output) == (None, 1)


def test_only_the_final_summary_line_counts():
    output = "3 passed in 0.10s\n...\n5 passed, 2 failed in 0.90s\n"
    assert parse_counts(output) == (5, 2)


def test_verbose_pytest_summary_is_parsed():
    assert parse_counts("====== 4 passed, 1 error in 1.00s ======\n") == (4, 1)


# -- journal 11: the worktree's own code is what gets tested ------------------


def test_worktree_src_is_first_on_pythonpath(tmp_path: Path, monkeypatch):
    (tmp_path / "src").mkdir()
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    env = command_env(tmp_path)
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert entries[:2] == [str(tmp_path / "src"), str(tmp_path)]
    assert entries[-1] == "/elsewhere"


async def test_the_test_command_sees_the_worktree_pythonpath(tmp_path: Path):
    (tmp_path / "src").mkdir()
    report = await run_tests("echo $PYTHONPATH", tmp_path, timeout_s=10)
    assert report.ok
    assert report.tail.startswith(str(tmp_path / "src"))


# -- journal 8: harness process trees die with the caller ---------------------


async def test_cancelling_a_call_kills_the_harness(fake_harnesses, project, tmp_path):
    pidfile = tmp_path / "harness.pid"
    fake_harnesses.configure({"implement": {"sleep": 30, "pidfile": str(pidfile)}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")

    task = asyncio.ensure_future(
        registry.get("codex").run("Implement the smallest change.", project)
    )
    deadline = time.monotonic() + 10
    while not pidfile.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    pid = int(pidfile.read_text())
    assert pid_alive(pid)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not pid_alive(pid)


async def test_a_timed_out_harness_is_gone_afterwards(fake_harnesses, project, tmp_path):
    pidfile = tmp_path / "harness.pid"
    fake_harnesses.configure({"implement": {"sleep": 30, "pidfile": str(pidfile)}})
    registry = RunnerRegistry(log_dir=tmp_path / "logs")

    result = await registry.get("codex").run(
        "Implement the smallest change.", project, timeout=timedelta(seconds=1)
    )

    assert not result.ok and "timed out" in result.error
    assert not pid_alive(int(pidfile.read_text()))


# -- alloy-c5v.2: RunContext.call reports the harness pid to the ledger ------


async def test_run_context_call_records_the_harness_pid_on_the_inflight_row(
    fake_harnesses, project, tmp_path
):
    """journal 9: while a harness is running, `inflight_calls.pid` must carry
    its actual pid -- not just at spawn time, but observably for the whole
    duration the call is in flight -- so reconcile/cancel can find it later
    even if `alloy run` itself is killed."""
    from types import SimpleNamespace

    from alloy.config import RoleSpec
    from alloy.runners import RunnerRegistry
    from alloy.runtime import RunContext
    from alloy.store import Store
    from support import make_bead

    pidfile = tmp_path / "harness.pid"
    fake_harnesses.configure({"implement": {"sleep": 30, "pidfile": str(pidfile)}})

    store = Store(tmp_path / "alloy.db")
    bead = make_bead()
    run_id = "run-1"
    store.create_run(
        run_id=run_id, bead_id=bead.id, thread_id=run_id, recipe="tdd-loop",
        repo=project, worktree=None, branch=None, log_dir=None,
    )
    ctx = RunContext(
        bead=bead,
        recipe=None,
        run_id=run_id,
        worktree=SimpleNamespace(path=project),
        worktrees=None,
        registry=RunnerRegistry(log_dir=tmp_path / "logs"),
        store=store,
        checkpointer=None,
        log_dir=tmp_path,
        beads=None,
    )

    task = asyncio.ensure_future(
        ctx.call("implement", RoleSpec(runner="codex"), "Implement the smallest change.")
    )
    try:
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        pid = int(pidfile.read_text())
        assert pid_alive(pid)

        active = store.active_calls(run_id)
        assert len(active) == 1
        assert active[0]["pid"] == pid
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_grandchildren_in_their_own_process_group_die_too(
    fake_harnesses, project, tmp_path
):
    """codex's sandbox helpers setpgid themselves; a bare killpg misses them."""
    grandchild = tmp_path / "grandchild.pid"
    fake_harnesses.configure(
        {"implement": {"sleep": 30, "detached_child_pidfile": str(grandchild)}}
    )
    registry = RunnerRegistry(log_dir=tmp_path / "logs")

    result = await registry.get("codex").run(
        "Implement the smallest change.", project, timeout=timedelta(seconds=1)
    )

    assert not result.ok
    pid = int(grandchild.read_text())
    deadline = time.monotonic() + 3
    while pid_alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not pid_alive(pid)


# -- journal 18: per-role fallback --------------------------------------------


def test_fallback_is_parsed_recursively():
    spec = RoleSpec.parse({
        "runner": "astra",
        "fallback": {"runner": "claude-write", "model": "fable",
                     "fallback": {"runner": "pi"}},
    })
    assert spec.fallback.runner == "claude-write"
    assert spec.fallback.model == "fable"
    assert spec.fallback.fallback.runner == "pi"
    assert RoleSpec.parse({"runner": "claude"}).fallback is None


async def test_fallback_runner_takes_over_when_the_primary_fails(
    project, alloy_home, fake_harnesses
):
    config = load_config()
    roles = dict(config.roles)
    roles["implement"] = replace(
        roles["implement"], fallback=RoleSpec(runner="claude-write", model="fable")
    )
    config = replace(config, roles=roles)
    fake_harnesses.configure(
        script(**{
            "implement@codex": [{"exit": 2, "stderr": "codex: rate limited", "text": ""}],
            "implement@claude": [implement_entry(succeed=True)],
        })
    )
    harness = make_harness(project, alloy_home, config=config)
    final = await harness.start()

    assert final["outcome"] == "done"
    implementers = [c["runner"] for c in fake_harnesses.calls_for("implement")]
    assert implementers == ["codex", "claude"]
    assert "--model" in fake_harnesses.calls_for("implement")[1]["argv"]
    assert final["attempts"][0]["implementer"] == "claude-write"
    # Both attempts are in the ledger; neither is hidden.
    calls = harness.store.agent_calls(harness.run_id)
    assert [(c["role"], c["runner"], c["ok"]) for c in calls if c["role"] == "implement"] == [
        ("implement", "codex", 0), ("implement", "claude-write", 1)
    ]


async def test_no_fallback_means_the_failure_stands(project, alloy_home, fake_harnesses):
    config = load_config()
    roles = dict(config.roles)
    roles["implement"] = replace(roles["implement"], fallback=None)
    fake_harnesses.configure(
        script(**{"implement@codex": [{"exit": 2, "stderr": "codex: down", "text": ""}],
                  "implement@claude": [implement_entry(succeed=True)]},
               judge=[judge_entry("abort", "give up")])
    )
    harness = make_harness(project, alloy_home, config=replace(config, roles=roles))
    await harness.start()

    assert [c["runner"] for c in fake_harnesses.calls_for("implement")] == ["codex"]


# -- journal 5 / 17: claiming and double runs ---------------------------------


@pytest.fixture
def engine(beads_project, alloy_home):
    return Engine.open(beads_project, alloy_home)


async def test_an_unknown_recipe_is_refused_before_the_bead_is_claimed(engine, beads_project):
    bead_id = bd_create(beads_project, "task", alloy_recipe="does-not-exist")

    with pytest.raises(EngineError, match="does-not-exist"):
        await engine.run(bead_id)

    assert engine.beads.show(bead_id).status == bd.STATUS_READY
    assert engine.store.latest_run_for_bead(bead_id) is None


async def test_a_bead_with_a_live_run_cannot_be_started_twice(engine, beads_project):
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")
    engine.beads.claim(bead_id)
    engine.store.create_run(
        run_id="live", bead_id=bead_id, thread_id="live", recipe="tdd-loop",
        repo=beads_project, worktree=None, branch=None, log_dir=None,
    )
    import subprocess
    import sys

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        engine.store.update_run("live", pid=holder.pid)
        with pytest.raises(EngineError, match="already being run"):
            await engine.run(bead_id)
        with pytest.raises(EngineError, match="already being run"):
            await engine.resume(bead_id)
    finally:
        holder.kill()
        holder.wait(timeout=10)


# -- journal 7: cancel stops the process --------------------------------------


async def test_cancel_terminates_the_owning_process(engine, beads_project):
    import subprocess
    import sys

    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")
    engine.beads.claim(bead_id)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    engine.store.create_run(
        run_id="owned", bead_id=bead_id, thread_id="owned", recipe="tdd-loop",
        repo=beads_project, worktree=None, branch=None, log_dir=None,
    )
    engine.store.update_run("owned", pid=holder.pid)
    try:
        assert engine.cancel(bead_id) is True
        assert not pid_alive(holder.pid)
        assert engine.store.get_run("owned")["status"] == "cancelled"
        assert engine.beads.show(bead_id).status == bd.STATUS_READY
    finally:
        if pid_alive(holder.pid):
            holder.kill()
        holder.wait(timeout=10)


# -- alloy-c5v.2: cancel kills orphaned harness process groups ---------------


async def test_cancel_kills_a_recorded_harness_pid_even_when_the_runs_own_pid_is_dead(
    engine, beads_project
):
    """journal 9: `alloy run` can die (SIGKILL, OOM) without cleaning up the
    harness process group it spawned. cancel() must still reach that group
    via the pid recorded on the inflight_calls row, even though the run's own
    pid -- the `alloy run` process itself -- is already gone."""
    import subprocess
    import sys

    from test_store import dead_pid

    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")
    engine.beads.claim(bead_id)
    engine.store.create_run(
        run_id="orphaned", bead_id=bead_id, thread_id="orphaned", recipe="tdd-loop",
        repo=beads_project, worktree=None, branch=None, log_dir=None,
    )
    engine.store.update_run("orphaned", pid=dead_pid())

    harness = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    engine.store.start_call(
        "call-orphaned", run_id="orphaned", bead_id=bead_id, role="implement",
        runner="codex", model=None,
    )
    engine.store.set_call_pid("call-orphaned", harness.pid)
    try:
        assert engine.cancel(bead_id) is True

        deadline = time.monotonic() + 5
        while pid_alive(harness.pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not pid_alive(harness.pid)
        assert engine.store.get_run("orphaned")["status"] == "cancelled"
    finally:
        if pid_alive(harness.pid):
            harness.kill()
        harness.wait(timeout=10)


# -- journal 14: paused time does not count against the wall clock ------------


def test_time_waiting_for_a_human_is_not_wall_time(alloy_home):
    from datetime import datetime, timezone

    from alloy.store import Store

    store = Store(alloy_home / "alloy.db")
    store.create_run(run_id="r", bead_id="b", thread_id="r", recipe="tdd-loop",
                     repo=Path("/repo"), worktree=None, branch=None, log_dir=None)
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    store.update_run("r", started_at=long_ago, paused_at=long_ago)

    store.mark_resumed("r")

    record = store.get_run("r")
    assert record["paused_at"] is None
    assert record["paused_s"] == pytest.approx(10 * 3600, abs=5)


async def test_a_resumed_run_does_not_immediately_hit_max_wall_time(
    project, alloy_home, fake_harnesses
):
    """Parked overnight at a human gate, then resumed: the loop must continue."""
    from datetime import datetime, timezone

    from langgraph.types import Command

    from alloy.config import Limits

    config = replace(load_config(), limits=Limits(max_iterations=5, max_consiliums=0,
                                                  max_agent_calls=100,
                                                  max_wall_time_minutes=30))
    fake_harnesses.configure(
        script(implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
               judge=[judge_entry("human", "which form?"), judge_entry("done")])
    )
    harness = make_harness(project, alloy_home, config=config)
    first = await harness.start()
    assert "__interrupt__" in first

    # Simulate the engine's bookkeeping around a long pause.
    night = datetime.now(timezone.utc) - timedelta(hours=8)
    harness.store.update_run(harness.run_id, started_at=(night - timedelta(minutes=5)).isoformat(),
                             paused_at=night.isoformat())
    harness.store.mark_resumed(harness.run_id)

    final = await harness.resume(Command(resume={"instructions": "carry on"}))

    assert final["outcome"] == "done"
    assert final.get("limit_hit") is None


async def test_time_a_run_spent_dead_is_not_wall_time(engine, beads_project, fake_harnesses):
    """An orphaned run adopted an hour after its process died has not been
    working for that hour."""
    from datetime import datetime, timezone

    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine.beads.claim(bead_id)
    engine.store.create_run(
        run_id="dead", bead_id=bead_id, thread_id="dead", recipe="tdd-loop",
        repo=beads_project, worktree=None, branch=None, log_dir=None,
    )
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    engine.store.update_run("dead", pid=999999, started_at=hour_ago)
    engine.store.update_run("dead", stage="implement")
    with engine.store.connect() as conn:  # updated_at is the owner's last write
        conn.execute("UPDATE runs SET updated_at = ? WHERE run_id = 'dead'", (hour_ago,))

    result = await engine.run(bead_id)

    assert result.run_id == "dead"
    assert engine.store.get_run("dead")["paused_s"] == pytest.approx(3600, abs=60)


# -- journal 16: the persisted stage reflects the end -------------------------


async def test_a_finished_run_records_its_final_stage(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    result = await engine.run(bead_id)

    assert engine.store.get_run(result.run_id)["stage"] == "finished"
