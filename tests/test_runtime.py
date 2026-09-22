"""RunContext.call's in-flight tracking (alloy-626, Component 1 item 1).

Exercises the real `RunContext.call` code path with stub runners standing in
for `runner.run()` -- no real model, no real CLI harness -- to prove: two
concurrent calls sharing one `run_id` are both visible in `Store.active_calls`
with distinct `call_id`s, and that a raised exception (including
`asyncio.CancelledError`) never leaves a row behind in `inflight_calls`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from alloy.config import RoleSpec
from alloy.models import AgentResult, RunnerUnavailable, utcnow
from alloy.runtime import RunContext
from alloy.store import Store
from support import load_config, make_bead


class _StaticRegistry:
    """Hands back one fixed runner regardless of the requested name."""

    def __init__(self, runner: Any) -> None:
        self._runner = runner

    def get(self, name: str) -> Any:
        return self._runner


class _MappedRegistry:
    """Hands back a different runner per requested name -- for concurrent
    critic-style fan-out, where each `ctx.call()` names its own runner."""

    def __init__(self, runners: dict[str, Any]) -> None:
        self._runners = runners

    def get(self, name: str) -> Any:
        return self._runners[name]


class BlockingRunner:
    """Blocks inside `run()` until released, so a test can observe the call
    while it is still in flight."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, prompt, cwd, *, model, timeout, structured_schema):
        self.started.set()
        await self.release.wait()
        now = utcnow()
        return AgentResult(
            runner="fake", model=model, ok=True, exit_code=0, text="ok",
            started_at=now, ended_at=now, duration_s=0.01,
        )


class FailingRunner:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run(self, prompt, cwd, *, model, timeout, structured_schema):
        raise self._exc


class HangingRunner:
    async def run(self, prompt, cwd, *, model, timeout, structured_schema):
        await asyncio.sleep(3600)


class UnavailableRunner:
    async def run(self, prompt, cwd, *, model, timeout, structured_schema):
        raise RunnerUnavailable("codex is not installed")


@pytest.fixture
def ctx_factory(tmp_path):
    store = Store(tmp_path / "alloy.db")
    bead = make_bead()
    run_id = "run-1"
    store.create_run(
        run_id=run_id, bead_id=bead.id, thread_id=run_id, recipe="tdd-loop",
        repo=tmp_path, worktree=None, branch=None, log_dir=None,
    )

    def make(registry: Any) -> RunContext:
        return RunContext(
            bead=bead,
            recipe=None,  # not touched by RunContext.call
            run_id=run_id,
            worktree=SimpleNamespace(path=tmp_path),
            worktrees=None,
            registry=registry,
            store=store,
            checkpointer=None,
            log_dir=tmp_path,
            beads=None,
        )

    return make


async def test_two_concurrent_calls_sharing_one_run_id_are_both_active_with_distinct_ids(
    ctx_factory,
):
    """Mirrors consilium's critic fan-out: several `ctx.call()`s in flight at once
    under one `run_id`."""
    runner_a, runner_b = BlockingRunner(), BlockingRunner()
    ctx = ctx_factory(_MappedRegistry({"critic-a": runner_a, "critic-b": runner_b}))

    task_a = asyncio.create_task(ctx.call("critic", RoleSpec(runner="critic-a"), "prompt a"))
    task_b = asyncio.create_task(ctx.call("critic", RoleSpec(runner="critic-b"), "prompt b"))
    await asyncio.wait_for(runner_a.started.wait(), timeout=5)
    await asyncio.wait_for(runner_b.started.wait(), timeout=5)

    active = ctx.store.active_calls(ctx.run_id)
    assert len(active) == 2
    call_ids = {row["call_id"] for row in active}
    assert len(call_ids) == 2  # distinct call_ids

    runner_a.release.set()
    await asyncio.wait_for(task_a, timeout=5)

    remaining = ctx.store.active_calls(ctx.run_id)
    assert len(remaining) == 1
    assert remaining[0]["call_id"] in call_ids  # the other call is still there, untouched

    runner_b.release.set()
    await asyncio.wait_for(task_b, timeout=5)
    assert ctx.store.active_calls(ctx.run_id) == []


async def test_an_exception_from_the_runner_leaves_no_inflight_row(ctx_factory):
    ctx = ctx_factory(_StaticRegistry(FailingRunner(RuntimeError("boom"))))

    with pytest.raises(RuntimeError, match="boom"):
        await ctx.call("implement", RoleSpec(runner="codex"), "prompt")

    assert ctx.store.active_calls(ctx.run_id) == []
    assert ctx.store.agent_calls(ctx.run_id) == []  # finish_call was never reached


async def test_a_cancelled_call_leaves_no_inflight_row(ctx_factory):
    ctx = ctx_factory(_StaticRegistry(HangingRunner()))

    task = asyncio.create_task(ctx.call("implement", RoleSpec(runner="codex"), "prompt"))
    await asyncio.sleep(0.05)
    assert len(ctx.store.active_calls(ctx.run_id)) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ctx.store.active_calls(ctx.run_id) == []
    assert ctx.store.agent_calls(ctx.run_id) == []


async def test_a_handled_runner_unavailable_still_finishes_the_call_and_leaves_no_inflight_row(
    ctx_factory,
):
    ctx = ctx_factory(_StaticRegistry(UnavailableRunner()))

    result = await ctx.call("implement", RoleSpec(runner="codex"), "prompt")

    assert result.ok is False
    assert ctx.store.active_calls(ctx.run_id) == []
    assert len(ctx.store.agent_calls(ctx.run_id)) == 1


async def test_remediate_without_bound_remediator_raises(ctx_factory):
    ctx = ctx_factory(_StaticRegistry(FailingRunner(RuntimeError("unused"))))
    assert getattr(ctx, "remediator", None) is None

    with pytest.raises(RuntimeError, match="no remediator bound"):
        await ctx.remediate("bug-bead-id")


async def test_check_limits_includes_three_child_calls_in_parent_agent_budget(ctx_factory, tmp_path):
    """After a child run with 3 agent calls, parent check_limits uses parent+child total."""
    from dataclasses import replace

    from alloy.runners import RunnerRegistry

    store = Store(tmp_path / "budget.db")
    parent_run_id = "parent-run"
    child_run_id = "child-run"
    store.create_run(
        run_id=parent_run_id, bead_id="parent", thread_id=parent_run_id, recipe="tdd-loop",
        repo=tmp_path, worktree=None, branch=None, log_dir=None,
    )
    store.create_run(
        run_id=child_run_id, bead_id="child", thread_id=f"{parent_run_id}/child",
        recipe="tdd-loop", repo=tmp_path, worktree=None, branch=None, log_dir=None,
    )
    store.update_run(child_run_id, parent_run_id=parent_run_id)

    bead = make_bead("parent")
    recipe = load_config()
    parent_calls = 4
    child_calls = 3
    for index in range(parent_calls):
        store.start_call(f"p-{index}", run_id=parent_run_id, bead_id=bead.id,
                         role="context", runner="codex", model=None)
        store.finish_call(
            f"p-{index}", run_id=parent_run_id, bead_id=bead.id, role="context",
            iteration=0,
            result=AgentResult(
                runner="codex", model=None, ok=True, exit_code=0, text="ok",
                started_at=utcnow(), ended_at=utcnow(), duration_s=0.0,
            ),
        )
    for index in range(child_calls):
        store.start_call(f"c-{index}", run_id=child_run_id, bead_id="child",
                         role="implement", runner="codex", model=None)
        store.finish_call(
            f"c-{index}", run_id=child_run_id, bead_id="child", role="implement",
            iteration=0,
            result=AgentResult(
                runner="codex", model=None, ok=True, exit_code=0, text="ok",
                started_at=utcnow(), ended_at=utcnow(), duration_s=0.0,
            ),
        )

    total = store.call_count(parent_run_id, include_children=True)
    assert total == parent_calls + child_calls

    recipe = replace(recipe, limits=replace(recipe.limits, max_agent_calls=total - 1))
    ctx = RunContext(
        bead=bead,
        recipe=recipe,
        run_id=parent_run_id,
        worktree=SimpleNamespace(path=tmp_path),
        worktrees=None,
        registry=RunnerRegistry(recipe.runners, log_dir=tmp_path),
        store=store,
        checkpointer=None,
        log_dir=tmp_path,
        beads=None,
    )
    breach = ctx.check_limits({"iteration": 0, "consiliums": 0})
    assert breach is not None
    assert "max_agent_calls reached" in breach
