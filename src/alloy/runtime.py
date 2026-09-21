"""Everything a recipe graph needs to touch the outside world.

Nodes stay declarative because all the I/O -- running a harness, running tests,
reading the diff, writing the ledger, enforcing limits -- lives here behind small
named methods.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from alloy.beads import BeadsClient, Bead, META_STAGE
from alloy.config import RecipeConfig, RoleSpec
from alloy.models import AgentResult, RunnerUnavailable, TestReport, utcnow
from alloy.runners import RunnerRegistry
from alloy.store import Store
from alloy.verify import run_tests
from alloy.worktree import Worktree, WorktreeManager


@dataclass
class RunContext:
    """Bound to exactly one bead, one worktree and one workflow run."""

    bead: Bead
    recipe: RecipeConfig
    run_id: str
    worktree: Worktree
    worktrees: WorktreeManager
    registry: RunnerRegistry
    store: Store
    checkpointer: Any
    log_dir: Path
    beads: BeadsClient | None = None
    started_monotonic: float = field(default_factory=time.monotonic)
    _stage: str = "starting"
    _iteration: int = 0

    # -- agent invocation -------------------------------------------------

    async def call(
        self,
        role: str,
        spec: RoleSpec,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        iteration: int = 0,
    ) -> AgentResult:
        """Run one harness, always inside this task's worktree, always recorded."""
        runner = self.registry.get(spec.runner)
        try:
            result = await runner.run(
                prompt,
                self.worktree.path,
                model=spec.model,
                timeout=spec.timeout,
                structured_schema=schema,
            )
        except RunnerUnavailable as exc:
            now = utcnow()
            result = AgentResult(
                runner=spec.runner, model=spec.model, ok=False, exit_code=127,
                started_at=now, ended_at=now, duration_s=0.0, error=str(exc),
            )
        self.store.record_agent_call(
            run_id=self.run_id, bead_id=self.bead.id, role=role,
            iteration=iteration, result=result,
        )
        return result

    # -- deterministic work -----------------------------------------------

    async def verify(self, command: str) -> TestReport:
        return await run_tests(
            command,
            self.worktree.path,
            timeout_s=self.recipe.verify.timeout_minutes * 60,
            log_dir=self.log_dir,
        )

    def diff(self, *, stat_only: bool = False) -> str:
        return self.worktrees.diff(self.worktree, stat_only=stat_only)

    # -- consilium --------------------------------------------------------

    def available_critics(self) -> list[RoleSpec]:
        """Critics whose CLI is actually installed. A missing harness is skipped,
        not fatal -- the consilium simply runs narrower."""
        return [
            spec for spec in self.recipe.consilium.critics
            if self.registry.available(spec.runner)
        ]

    def critic_spec(self, runner: str, model: str | None) -> RoleSpec:
        for spec in self.recipe.consilium.critics:
            if spec.runner == runner and spec.model == model:
                return spec
        return RoleSpec(runner=runner, model=model)

    # -- deterministic limits ---------------------------------------------

    def budget(self, state: dict[str, Any]) -> int:
        """How many times the configured budget has been granted.

        Starts at 1. Each human resume grants one more window, so an explicit
        human decision -- and only that -- can extend a limit.
        """
        return 1 + int(state.get("budget_extensions", 0) or 0)

    def check_limits(self, state: dict[str, Any]) -> str | None:
        """Return a description of the first limit breached, else None."""
        limits = self.recipe.limits
        multiplier = self.budget(state)

        iteration = int(state.get("iteration", 0) or 0)
        allowed_iterations = limits.max_iterations * multiplier
        if iteration >= allowed_iterations:
            return f"max_iterations reached ({iteration}/{allowed_iterations})"

        consiliums = int(state.get("consiliums", 0) or 0)
        if consiliums > limits.max_consiliums * multiplier:
            return f"max_consiliums reached ({consiliums}/{limits.max_consiliums * multiplier})"

        calls = self.store.call_count(self.run_id)
        allowed_calls = limits.max_agent_calls * multiplier
        if calls >= allowed_calls:
            return f"max_agent_calls reached ({calls}/{allowed_calls})"

        elapsed = self.elapsed()
        allowed_wall = timedelta(minutes=limits.max_wall_time_minutes * multiplier)
        if elapsed > allowed_wall:
            return (
                f"max_wall_time reached ({elapsed.total_seconds() / 60:.0f}m/"
                f"{allowed_wall.total_seconds() / 60:.0f}m)"
            )
        return None

    def elapsed(self) -> timedelta:
        """Wall time since the run was first created, across restarts."""
        record = self.store.get_run(self.run_id)
        if record and record.get("started_at"):
            from datetime import datetime

            try:
                started = datetime.fromisoformat(record["started_at"])
            except ValueError:
                return timedelta(seconds=time.monotonic() - self.started_monotonic)
            return utcnow() - started
        return timedelta(seconds=time.monotonic() - self.started_monotonic)

    def limits_note(self, state: dict[str, Any]) -> str:
        limits = self.recipe.limits
        multiplier = self.budget(state)
        return (
            f"{limits.max_iterations * multiplier} iterations allowed, "
            f"{limits.max_consiliums * multiplier} consilium(s) allowed, "
            f"{self.store.call_count(self.run_id)}/"
            f"{limits.max_agent_calls * multiplier} agent calls used, "
            f"{self.elapsed().total_seconds() / 60:.0f}m of "
            f"{limits.max_wall_time_minutes * multiplier:.0f}m elapsed"
        )

    # -- progress reporting -----------------------------------------------

    def set_stage(self, stage: str, *, iteration: int | None = None) -> None:
        self._stage = stage
        if iteration is not None:
            self._iteration = iteration
        self.store.update_run(
            self.run_id, stage=stage, iteration=self._iteration,
        )
        if self.beads is not None:
            try:
                self.beads.set_metadata(self.bead.id, {META_STAGE: stage})
            except Exception:
                pass  # progress reporting must never break execution

    def set_tests_summary(self, summary: str) -> None:
        self.store.update_run(self.run_id, tests_summary=summary)

    def set_consiliums(self, count: int) -> None:
        self.store.update_run(self.run_id, consiliums=count)
