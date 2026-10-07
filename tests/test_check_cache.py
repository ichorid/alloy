"""No redundant check re-runs: a command that already passed in this run
against the same worktree fingerprint is answered from the cache."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from support import load_config, make_bead

from alloy.models import CheckRequest
from alloy.runtime import RunContext
from alloy.store import Store
from alloy.verify import check_logs
from alloy.worktree import tree_fingerprint


@pytest.fixture
def ctx(project: Path, tmp_path: Path) -> RunContext:
    store = Store(tmp_path / "alloy.db")
    store.create_run(
        run_id="run-1",
        bead_id="t-1",
        thread_id="run-1",
        recipe="tdd-loop",
        repo=project,
        worktree=project,
        branch="",
        log_dir=tmp_path / "logs",
    )
    return RunContext(
        bead=make_bead(),
        recipe=load_config(),
        run_id="run-1",
        worktree=SimpleNamespace(path=project),
        worktrees=None,
        registry=None,
        store=store,
        checkpointer=None,
        log_dir=tmp_path / "logs",
    )


def _counting(tmp_path: Path, exit_code: int = 0) -> tuple[str, Path]:
    """A check that records each real execution outside the worktree."""
    counter = tmp_path / "executions.txt"
    return f"echo ran >> {counter}; exit {exit_code}", counter


def _runs(counter: Path) -> int:
    return len(counter.read_text().splitlines()) if counter.exists() else 0


async def test_same_passing_command_on_unchanged_tree_is_not_rerun(ctx, tmp_path):
    command, counter = _counting(tmp_path)
    first = await ctx.run_check(CheckRequest(command=command, kind="targeted"))
    second = await ctx.run_check(CheckRequest(command=command, kind="regression", purpose="full suite"))

    assert _runs(counter) == 1
    assert first.ok and not first.cached
    assert second.ok and second.cached
    assert second.duration_s == 0.0
    assert second.kind == "regression" and second.purpose == "full suite"
    assert second.fingerprint == first.fingerprint == tree_fingerprint(ctx.worktree.path)
    assert "cached" in second.headline()
    logs = check_logs(tmp_path / "logs")
    assert len(logs) == 2  # the cached answer is in the ledger too
    assert "cached=true" in Path(logs[-1]["log_path"]).read_text()


async def test_a_code_change_forces_a_rerun(ctx, project, tmp_path):
    command, counter = _counting(tmp_path)
    await ctx.run_check(CheckRequest(command=command))
    (project / "mypkg" / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    again = await ctx.run_check(CheckRequest(command=command))
    assert _runs(counter) == 2 and not again.cached


async def test_a_new_untracked_file_forces_a_rerun(ctx, project, tmp_path):
    command, counter = _counting(tmp_path)
    await ctx.run_check(CheckRequest(command=command))
    (project / "tests" / "test_new.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    await ctx.run_check(CheckRequest(command=command))
    assert _runs(counter) == 2


async def test_alloy_state_does_not_perturb_the_fingerprint(ctx, project, tmp_path):
    command, counter = _counting(tmp_path)
    await ctx.run_check(CheckRequest(command=command))
    (project / ".alloy").mkdir()
    (project / ".alloy" / "alloy.db").write_text("changed", encoding="utf-8")
    second = await ctx.run_check(CheckRequest(command=command))
    assert _runs(counter) == 1 and second.cached


async def test_failures_and_different_commands_always_rerun(ctx, tmp_path):
    failing, counter = _counting(tmp_path, exit_code=1)
    await ctx.run_check(CheckRequest(command=failing))
    red = await ctx.run_check(CheckRequest(command=failing))
    assert _runs(counter) == 2 and not red.cached and not red.ok

    other = f"echo other >> {counter}"
    await ctx.run_check(CheckRequest(command=other))
    assert _runs(counter) == 3


async def test_a_check_that_changes_the_tree_is_not_cached(ctx, project, tmp_path):
    counter = tmp_path / "executions.txt"
    command = f"echo ran >> {counter}; date +%s%N > {project}/generated.txt"
    await ctx.run_check(CheckRequest(command=command))
    await ctx.run_check(CheckRequest(command=command))
    assert _runs(counter) == 2


async def test_cache_is_per_run(ctx, tmp_path):
    command, counter = _counting(tmp_path)
    await ctx.run_check(CheckRequest(command=command))
    ctx.store.create_run(
        run_id="run-2",
        bead_id="t-1",
        thread_id="run-2",
        recipe="tdd-loop",
        repo=ctx.worktree.path,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    ctx.run_id = "run-2"
    await ctx.run_check(CheckRequest(command=command))
    assert _runs(counter) == 2
