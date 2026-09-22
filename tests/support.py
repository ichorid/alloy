"""Helpers for driving a recipe graph in tests without an engine or Beads."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from alloy.checkpoints import open_checkpointer
from alloy.config import RecipeConfig, RoleSpec
from alloy.beads import Bead
from alloy.recipes import tdd_loop
from alloy.runners import RunnerRegistry
from alloy.runtime import RunContext
from alloy.store import Store
from alloy.worktree import WorktreeManager

BASE_RECIPE = Path(__file__).parents[1] / "src" / "alloy" / "recipes" / "tdd-loop.yaml"


def scope_config(**overrides: Any) -> RecipeConfig:
    """Recipe config with a scope role (jev primary, claude fallback)."""
    config = load_config()
    roles = dict(config.roles)
    fallback = overrides.pop(
        "fallback", RoleSpec(runner="claude", model="sonnet", timeout_minutes=5),
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
    ) -> None:
        self.bead = bead
        self.recipe_config = config
        self.run_id = run_id
        self.store = store
        self.project = Path(project)
        self.alloy_home = Path(alloy_home)
        self.beads = beads
        self.initial_state_overrides = initial_state_overrides or {}
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
        )

    async def start(self) -> dict:
        async with open_checkpointer(self.alloy_home / "workflows.db") as checkpointer:
            ctx = self.context(checkpointer)
            graph = tdd_loop.build_graph(ctx)
            state = tdd_loop.initial_state(ctx)
            if self.initial_state_overrides:
                state = {**state, **self.initial_state_overrides}
            return await graph.ainvoke(state, self.config)

    async def resume(self, payload: Any = None) -> dict:
        async with open_checkpointer(self.alloy_home / "workflows.db") as checkpointer:
            ctx = self.context(checkpointer)
            graph = tdd_loop.build_graph(ctx)
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
) -> Harness:
    bead = bead or make_bead()
    config = config or load_config()
    run_id = run_id or uuid.uuid4().hex
    store = store or Store(alloy_home / "alloy.db")

    worktrees = WorktreeManager(repo=project, root=alloy_home / "worktrees")
    worktree = worktrees.ensure(bead.id)

    if store.get_run(run_id) is None:
        store.create_run(
            run_id=run_id, bead_id=bead.id, thread_id=run_id, recipe=config.name,
            repo=project, worktree=worktree.path, branch=worktree.branch,
            log_dir=alloy_home / "logs" / run_id,
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
    )
