"""Per-run agent call limits: remediation children do not spend the parent budget (alloy-8by.1)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from support import load_config, make_bead

from alloy.models import AgentResult, utcnow
from alloy.runners import RunnerRegistry
from alloy.runtime import RunContext
from alloy.store import Store

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_PY = _REPO_ROOT / "src" / "alloy" / "runtime.py"


def _agent_result() -> AgentResult:
    return AgentResult(
        runner="codex",
        model=None,
        ok=True,
        exit_code=0,
        text="ok",
        started_at=utcnow(),
        ended_at=utcnow(),
        duration_s=0.0,
    )


def _seed_calls(
    store: Store,
    *,
    run_id: str,
    bead_id: str,
    count: int,
    prefix: str,
) -> None:
    for index in range(count):
        call_id = f"{prefix}-{index}"
        store.start_call(
            call_id,
            run_id=run_id,
            bead_id=bead_id,
            role="context",
            runner="codex",
            model=None,
        )
        store.finish_call(
            call_id,
            run_id=run_id,
            bead_id=bead_id,
            role="context",
            iteration=0,
            result=_agent_result(),
        )


def _run_context(
    store: Store,
    tmp_path,
    *,
    run_id: str,
    bead_id: str,
    max_agent_calls: int,
) -> RunContext:
    recipe = replace(
        load_config(),
        limits=replace(load_config().limits, max_agent_calls=max_agent_calls),
    )
    return RunContext(
        bead=make_bead(bead_id),
        recipe=recipe,
        run_id=run_id,
        worktree=SimpleNamespace(path=tmp_path),
        worktrees=None,
        registry=RunnerRegistry(recipe.runners, log_dir=tmp_path),
        store=store,
        checkpointer=None,
        log_dir=tmp_path,
        beads=None,
    )


def _parent_child_store(tmp_path) -> tuple[Store, str, str]:
    store = Store(tmp_path / "per-run-budget.db")
    parent_run_id = "parent-run"
    child_run_id = "child-run"
    store.create_run(
        run_id=parent_run_id,
        bead_id="parent",
        thread_id=parent_run_id,
        recipe="tdd-loop",
        repo=tmp_path,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    store.create_run(
        run_id=child_run_id,
        bead_id="child",
        thread_id=f"{parent_run_id}/child",
        recipe="tdd-loop",
        repo=tmp_path,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    store.update_run(child_run_id, parent_run_id=parent_run_id)
    return store, parent_run_id, child_run_id


async def test_parent_check_limits_ignores_child_calls_14_own_12_child_max_20(tmp_path):
    """Parent with 14 own calls and a child with 12 must stay under max_agent_calls=20."""
    store, parent_run_id, child_run_id = _parent_child_store(tmp_path)
    _seed_calls(store, run_id=parent_run_id, bead_id="parent", count=14, prefix="p")
    _seed_calls(store, run_id=child_run_id, bead_id="child", count=12, prefix="c")

    assert store.call_count(parent_run_id) == 14
    assert store.call_count(child_run_id) == 12
    assert store.call_count(parent_run_id, include_children=True) == 26

    parent_ctx = _run_context(
        store,
        tmp_path,
        run_id=parent_run_id,
        bead_id="parent",
        max_agent_calls=20,
    )
    assert parent_ctx.check_limits({"iteration": 0, "consiliums": 0}) is None


async def test_child_check_limits_breaches_on_own_calls_only(tmp_path):
    """Child remediation is capped by its own agent-call count, not the parent's."""
    store, parent_run_id, child_run_id = _parent_child_store(tmp_path)
    _seed_calls(store, run_id=parent_run_id, bead_id="parent", count=14, prefix="p")
    _seed_calls(store, run_id=child_run_id, bead_id="child", count=21, prefix="c")

    child_ctx = _run_context(
        store,
        tmp_path,
        run_id=child_run_id,
        bead_id="child",
        max_agent_calls=20,
    )
    breach = child_ctx.check_limits({"iteration": 0, "consiliums": 0})
    assert breach is not None
    assert "max_agent_calls reached" in breach

    parent_ctx = _run_context(
        store,
        tmp_path,
        run_id=parent_run_id,
        bead_id="parent",
        max_agent_calls=20,
    )
    assert parent_ctx.check_limits({"iteration": 0, "consiliums": 0}) is None


def test_runtime_limits_do_not_use_include_children_rollup():
    """Limit enforcement in runtime.py must count only the current run's calls."""
    text = _RUNTIME_PY.read_text(encoding="utf-8")
    assert "include_children=True" not in text
