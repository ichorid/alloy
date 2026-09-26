"""Helpers for driving a recipe graph in tests without an engine or Beads."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from alloy.checkpoints import open_checkpointer
from alloy.config import RecipeConfig, RoleSpec, load_recipe
from alloy.beads import Bead
from alloy.engine import RunResult
from alloy.models import utcnow
from alloy.recipes import tdd_loop
from alloy.runners import RunnerRegistry
from alloy.runtime import RunContext
from alloy.store import Store
from alloy.worktree import WorktreeManager

BASE_RECIPE = Path(__file__).parents[1] / "src" / "alloy" / "recipes" / "tdd-loop.yaml"
LAND_RECIPE_NAME = "land"


def scope_config(**overrides: Any) -> RecipeConfig:
    """Recipe config with a scope role (jev primary, cursor fallback)."""
    config = load_config()
    roles = dict(config.roles)
    fallback = overrides.pop(
        "fallback",
        RoleSpec(runner="cursor", model="composer-2.5", timeout_minutes=5),
    )
    roles["scope"] = RoleSpec(
        runner=overrides.pop("runner", "jev"),
        model=overrides.pop("model", "jev-latest"),
        timeout_minutes=5,
        fallback=fallback,
    )
    return replace(config, roles=roles, **overrides)


def load_config(**overrides: Any) -> RecipeConfig:
    raw = yaml.safe_load(BASE_RECIPE.read_text())
    config = RecipeConfig.parse(raw, source=BASE_RECIPE)
    roles = dict(config.roles)
    if "estimate" in roles:
        # No jev fake on PATH; exercise estimate through the claude harness.
        est = roles["estimate"]
        roles["estimate"] = replace(
            est,
            runner="claude",
            model=est.model,
            fallback=None,
        )
        config = replace(config, roles=roles)
    if "acceptance" in roles:
        # No jev fake on PATH; exercise acceptance through the claude harness.
        acceptance = roles["acceptance"]
        roles["acceptance"] = replace(
            acceptance,
            runner="claude",
            model=acceptance.model,
            fallback=None,
        )
        config = replace(config, roles=roles)
    return replace(config, **overrides) if overrides else config


def load_land_config(**overrides: Any) -> RecipeConfig:
    """Load the land recipe YAML with jev roles swapped to claude for harness tests."""
    config = load_recipe(LAND_RECIPE_NAME)
    roles = dict(config.roles)
    for role_name, spec in list(roles.items()):
        if spec.runner == "jev":
            roles[role_name] = replace(
                spec,
                runner="claude",
                model=spec.model,
                fallback=None,
            )
    config = replace(config, roles=roles)
    return replace(config, **overrides) if overrides else config


def make_bead(bead_id: str = "t-1", **fields: Any) -> Bead:
    defaults = dict(
        id=bead_id,
        title="add slugify",
        description="Add a slugify() helper to mypkg.",
        acceptance_criteria="slugify('Hello World') == 'hello-world'",
        status="implementing",
        metadata={"alloy_recipe": "tdd-loop"},
    )
    defaults.update(fields)
    return Bead(**defaults)


@dataclass
class RemediationStep:
    """One scripted outcome for :class:`FakeRemediator`."""

    outcome: str = "done"
    reason: str = ""
    sleep_s: float = 0.0
    fix_writes: list[dict[str, str]] | None = None


class FakeRemediator:
    """Stand-in for ``Engine.run_child`` in graph-level remediation tests."""

    def __init__(
        self,
        *,
        worktree_path: Path,
        steps: list[RemediationStep] | None = None,
    ) -> None:
        self.worktree_path = Path(worktree_path)
        self.steps = list(steps or [RemediationStep()])
        self.calls: list[dict[str, Any]] = []
        self._index = 0

    async def __call__(self, bug_bead_id: str) -> RunResult:
        step = self.steps[min(self._index, len(self.steps) - 1)]
        self._index += 1
        started = utcnow()
        if step.sleep_s:
            await asyncio.sleep(step.sleep_s)
        if step.fix_writes:
            for write in step.fix_writes:
                path = self.worktree_path / write["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(write["content"], encoding="utf-8")
        ended = utcnow()
        run_id = f"fake-child-{uuid.uuid4().hex[:8]}"
        self.calls.append(
            {
                "bead_id": bug_bead_id,
                "run_id": run_id,
                "outcome": step.outcome,
                "reason": step.reason,
                "started_at": started.isoformat(),
                "ended_at": ended.isoformat(),
            }
        )
        return RunResult(
            bead_id=bug_bead_id,
            run_id=run_id,
            outcome=step.outcome,
            reason=step.reason,
            worktree=str(self.worktree_path),
        )


def bind_fake_remediator(harness: Harness, **kwargs: Any) -> FakeRemediator:
    """Attach a :class:`FakeRemediator` to a harness's worktree."""
    worktree = harness.worktrees.ensure(harness.bead.id)
    remediator = FakeRemediator(worktree_path=worktree.path, **kwargs)
    harness.remediator = remediator
    return remediator


class Harness:
    """Drives one task's graph.

    Each call opens the checkpointer and rebuilds the graph from scratch, exactly
    as the engine does -- so a resume in a test is a real resume, not a
    continuation of an object that stayed alive in memory.
    """

    def __init__(
        self,
        *,
        bead: Bead,
        config: RecipeConfig,
        run_id: str,
        store: Store,
        project: Path,
        alloy_home: Path,
        beads: Any = None,
        initial_state_overrides: dict[str, Any] | None = None,
        remediator: Callable[[str], Awaitable[RunResult]] | None = None,
        recipe_name: str = "tdd-loop",
    ) -> None:
        self.bead = bead
        self.recipe_config = config
        self.recipe_name = recipe_name
        self.run_id = run_id
        self.store = store
        self.project = Path(project)
        self.alloy_home = Path(alloy_home)
        self.beads = beads
        self.initial_state_overrides = initial_state_overrides or {}
        self.remediator = remediator
        self.thread_id = run_id
        self.worktrees = WorktreeManager(repo=self.project, root=self.alloy_home / "worktrees")

    @property
    def config(self) -> dict:
        return {"configurable": {"thread_id": self.thread_id}, "recursion_limit": 200}

    def context(self, checkpointer: Any) -> RunContext:
        worktree = self.worktrees.ensure(self.bead.id)
        log_dir = self.alloy_home / "logs" / self.run_id
        log_dir.mkdir(parents=True, exist_ok=True)
        return RunContext(
            bead=self.bead,
            recipe=self.recipe_config,
            run_id=self.run_id,
            worktree=worktree,
            worktrees=self.worktrees,
            registry=RunnerRegistry(self.recipe_config.runners, log_dir=log_dir),
            store=self.store,
            checkpointer=checkpointer,
            log_dir=log_dir,
            beads=self.beads,
            remediator=self.remediator,
        )

    async def start(self) -> dict:
        async with open_checkpointer(self.alloy_home / "workflows.db") as checkpointer:
            ctx = self.context(checkpointer)
            from alloy import recipes

            recipe = recipes.get(self.recipe_name)
            graph = recipe.build_graph(ctx)
            state = recipe.initial_state(ctx)
            if self.initial_state_overrides:
                state = {**state, **self.initial_state_overrides}
            return await graph.ainvoke(state, self.config)

    async def resume(self, payload: Any = None) -> dict:
        async with open_checkpointer(self.alloy_home / "workflows.db") as checkpointer:
            ctx = self.context(checkpointer)
            from alloy import recipes

            recipe = recipes.get(self.recipe_name)
            graph = recipe.build_graph(ctx)
            return await graph.ainvoke(payload, self.config)

    async def snapshot(self) -> dict | None:
        from alloy.checkpoints import read_checkpoint

        return read_checkpoint(self.alloy_home / "workflows.db", self.thread_id)

    def close(self) -> None:
        """Kept so tests can read as open/close pairs; nothing is held open."""


def make_harness(
    project: Path,
    alloy_home: Path,
    *,
    bead: Bead | None = None,
    config: RecipeConfig | None = None,
    run_id: str | None = None,
    store: Store | None = None,
    beads: Any = None,
    initial_state_overrides: dict[str, Any] | None = None,
    remediator: Callable[[str], Awaitable[RunResult]] | None = None,
    parent_run_id: str | None = None,
    recipe_name: str = "tdd-loop",
) -> Harness:
    bead = bead or make_bead()
    if config is None:
        config = load_land_config() if recipe_name == LAND_RECIPE_NAME else load_config()
    run_id = run_id or uuid.uuid4().hex
    store = store or Store(alloy_home / "alloy.db")

    worktrees = WorktreeManager(repo=project, root=alloy_home / "worktrees")
    worktree = worktrees.ensure(bead.id)

    if store.get_run(run_id) is None:
        store.create_run(
            run_id=run_id,
            bead_id=bead.id,
            thread_id=run_id,
            recipe=config.name,
            repo=project,
            worktree=worktree.path,
            branch=worktree.branch,
            log_dir=alloy_home / "logs" / run_id,
            parent_run_id=parent_run_id,
        )

    return Harness(
        bead=bead,
        config=config,
        run_id=run_id,
        store=store,
        project=project,
        alloy_home=alloy_home,
        beads=beads,
        initial_state_overrides=initial_state_overrides,
        remediator=remediator,
        recipe_name=recipe_name,
    )


def wait_for_role(fake_harnesses: Any, role: str, timeout: float) -> None:
    """Block until the fake CLI has logged a call for ``role`` (polls calls.jsonl).

    The fake appends to calls.jsonl before it sleeps, so seeing the call means the
    previous node's checkpoint has already been committed.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fake_harnesses.calls_for(role):
            return
        time.sleep(0.1)
    raise AssertionError(f"role {role!r} never ran; saw {fake_harnesses.calls}")


async def await_role(fake_harnesses: Any, role: str, timeout: float) -> None:
    """Async twin of :func:`wait_for_role`; yields so an in-process engine task can run."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fake_harnesses.calls_for(role):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"role {role!r} never ran; saw {fake_harnesses.calls}")


async def await_cancelled_task(
    task: asyncio.Task,
    *,
    budget_s: float = 6.0,
) -> float:
    """Await a cancelled graph task; fail if checkpointer teardown exceeds ``budget_s``."""
    import pytest

    from conftest import SIMULATED_CLOSE_STALL_S

    started = time.monotonic()
    try:
        await asyncio.wait_for(task, timeout=budget_s)
        pytest.fail("expected CancelledError from cancelled graph invocation")
    except asyncio.TimeoutError:
        pytest.fail(
            "cancel+await exceeded checkpointer teardown budget "
            f"({budget_s}s); open_checkpointer close is not bounded under cancellation"
        )
    except asyncio.CancelledError:
        return time.monotonic() - started
    finally:
        if not task.done():
            await asyncio.wait_for(task, timeout=SIMULATED_CLOSE_STALL_S + 2)
