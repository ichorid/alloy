"""Everything a recipe graph needs to touch the outside world.

Nodes stay declarative because all the I/O -- running a harness, running tests,
reading the diff, writing the ledger, enforcing limits -- lives here behind small
named methods.
"""

from __future__ import annotations

import inspect
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from alloy.beads import BeadsClient, Bead, META_COMPLEXITY_ESTIMATED, META_STAGE
from alloy.config import RecipeConfig, RoleSpec, resolve_max_agent_calls
from alloy.models import (
    UNAVAILABLE_KEY,
    is_unavailable,
    AgentResult,
    CheckRequest,
    CheckResult,
    ProjectMemory,
    ProjectSnapshot,
    RunnerUnavailable,
    utcnow,
)
from alloy.limits import parse_retry_at
from alloy.paths import project_brief
from alloy.runners import RunnerRegistry
from alloy.store import Store
from alloy.verify import run_check
from alloy.worktree import Worktree, WorktreeManager

log = logging.getLogger("alloy.runtime")


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
    # Bound by the engine (Engine.run_child); recipes never import the engine.
    remediator: Callable[[str], Awaitable[Any]] | None = None
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
        resume_session: str | None = None,
    ) -> AgentResult:
        """Run one harness, always inside this task's worktree, always recorded.

        If the role names a fallback and the primary runner is unavailable,
        times out, or exits non-zero, the same prompt goes to the fallback.
        Every attempt is recorded, so the ledger shows what actually ran.

        `resume_session` continues an earlier session of the primary runner;
        a fallback is a different harness and cannot resume it, so the
        fallback attempt always starts cold.
        """
        result = await self._call_one(
            role, spec, prompt, schema=schema, iteration=iteration,
            resume_session=resume_session,
        )
        while not result.ok and spec.fallback is not None:
            log.warning(
                "%s: %s failed (%s); falling back to %s",
                role, spec.label, (result.error or f"exit {result.exit_code}")[:200],
                spec.fallback.label,
            )
            if resume_session is not None:
                log.warning(
                    "%s: dropping resume session %s -- %s cannot resume a %s session",
                    role, resume_session, spec.fallback.label, spec.label,
                )
                resume_session = None
            spec = spec.fallback
            result = await self._call_one(
                role, spec, prompt, schema=schema, iteration=iteration,
                resume_session=None,
            )
        return result

    async def _call_one(
        self,
        role: str,
        spec: RoleSpec,
        prompt: str,
        *,
        schema: dict[str, Any] | None,
        iteration: int,
        resume_session: str | None = None,
    ) -> AgentResult:
        """One harness invocation, visible in `inflight_calls` while it runs
        and moved into `agent_calls` in the same transaction when it ends."""
        log.info("%s: calling %s (iteration %d)", role, spec.label, iteration)
        call_id = uuid.uuid4().hex
        self.store.start_call(
            call_id, run_id=self.run_id, bead_id=self.bead.id, role=role,
            runner=spec.runner, model=spec.model,
        )
        finished = False
        try:
            try:
                runner = self.registry.get(spec.runner)
                extra: dict[str, Any] = {}
                if _accepts_on_spawn(runner):
                    # journal 9: the row exists before the harness does; the
                    # pid lands on it as soon as the spawn succeeds.
                    extra["on_spawn"] = lambda pid: self.store.set_call_pid(call_id, pid)
                if spec.effort is not None and _accepts_kwarg(runner, "effort"):
                    extra["effort"] = spec.effort
                if resume_session is not None and _accepts_kwarg(runner, "resume_session"):
                    extra["resume_session"] = resume_session
                result = await runner.run(
                    prompt,
                    self.worktree.path,
                    model=spec.model,
                    timeout=spec.timeout,
                    structured_schema=schema,
                    **extra,
                )
            except RunnerUnavailable as exc:
                now = utcnow()
                result = AgentResult(
                    runner=spec.runner, model=spec.model, ok=False, exit_code=127,
                    started_at=now, ended_at=now, duration_s=0.0, error=str(exc),
                )
            # Recorded in usage_json so `alloy logs` can show which calls
            # continued an earlier session.
            result.usage = {**(result.usage or {}), "resumed": resume_session is not None}
            if not result.ok and result.retry_at is None:
                # journal 38: "resets 1:20am" is local wall-clock time; keep it
                # timezone-aware so the scheduler can compare it with utcnow().
                retry_at = parse_retry_at(result.error or result.text, now=datetime.now())
                if retry_at is not None:
                    result.retry_at = retry_at.astimezone()
            if is_unavailable(result):
                # The harness never ran (missing binary, spend or rate limit):
                # the ledger keeps the row but it does not spend the run's
                # agent-call budget.
                result.usage = {**result.usage, UNAVAILABLE_KEY: True}
            self.store.finish_call(
                call_id, run_id=self.run_id, bead_id=self.bead.id, role=role,
                iteration=iteration, result=result,
            )
            finished = True
        finally:
            if not finished:
                self.store.discard_call(call_id)
        log.info(
            "%s: %s %s in %.0fs%s", role, result.runner,
            "ok" if result.ok else f"failed (exit {result.exit_code})", result.duration_s,
            f" -- {result.error[:200]}" if result.error else "",
        )
        return result

    # -- remediation ------------------------------------------------------

    @property
    def is_child(self) -> bool:
        """True when this run is itself a remediation child (depth one)."""
        record = self.store.get_run(self.run_id)
        return bool(record and record.get("parent_run_id"))

    async def remediate(self, bug_bead_id: str) -> Any:
        """Run a bug bead as a child of this run and merge its fix in here."""
        if self.remediator is None:
            raise RuntimeError("no remediator bound")
        return await self.remediator(bug_bead_id)

    # -- project memory ---------------------------------------------------

    def project_memory(self) -> ProjectMemory | None:
        """The project memories for this run, or None when memory is disabled,
        no beads client is bound, or bd failed.

        Read from `bd memories` once, at run start, by `initial_state`; what
        the run needs from it lives in graph state so resumes and every
        iteration reuse the same snapshot."""
        spec = self.recipe.memory
        if not spec.enabled or self.beads is None:
            return None
        try:
            memories = self.beads.memories()
        except Exception:
            log.warning("bd memories failed; running without project memory", exc_info=True)
            return None
        return ProjectMemory.from_raw(memories, spec)

    def memory_block(self) -> str:
        """The rendered project memory block for this run's prompts; empty
        when there is no memory to render."""
        memory = self.project_memory()
        return memory.render() if memory is not None else ""

    # -- project context --------------------------------------------------

    def project_context(self, state: Mapping[str, Any] | None = None) -> str:
        """The project context packet for the scope and triage roles.

        Rebuilt on every call because the bead graph moves while a run is
        paused; `state` (the graph state, when the caller has it) supplies the
        attempt history and remediations of this run."""
        from alloy.recipes.tdd_loop import render_project_context

        snapshot = ProjectSnapshot()
        if self.beads is not None:
            try:
                snapshot = self.beads.project_snapshot(self.bead.id)
            except Exception:
                log.warning("project snapshot for %s failed", self.bead.id, exc_info=True)
        state = state or {}
        iteration = state.get("iteration")
        if iteration is None:
            record = self.store.get_run(self.run_id)
            iteration = record.get("iteration") if record else None
        return render_project_context(
            snapshot,
            project_brief(self.worktrees.repo),
            list(state.get("attempts") or []),
            list(state.get("remediations") or []),
            iteration=iteration,
        )

    # -- deterministic work -----------------------------------------------

    async def run_check(self, request: CheckRequest) -> CheckResult:
        index = len(list(self.log_dir.glob("check-*.log"))) if self.log_dir.is_dir() else 0
        return await run_check(
            request,
            self.worktree.path,
            timeout_s=self.recipe.verification.max_command_timeout_minutes * 60,
            log_dir=self.log_dir,
            index=index,
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

    def agent_call_limit(self, state: Mapping[str, Any]) -> int:
        """Tier-scaled agent-call ceiling for this run, before budget extensions."""
        return resolve_max_agent_calls(
            self.recipe, state.get("complexity"), state.get("memory_calibration", "")
        )

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
        allowed_calls = self.agent_call_limit(state) * multiplier
        if calls >= allowed_calls:
            return f"max_agent_calls reached ({calls}/{allowed_calls})"

        if self.total_checks(state) >= self.total_checks_allowed(state):
            return "max_total_checks reached"

        elapsed = self.elapsed()
        allowed_wall = timedelta(minutes=limits.max_wall_time_minutes * multiplier)
        if elapsed > allowed_wall:
            return (
                f"max_wall_time reached ({elapsed.total_seconds() / 60:.0f}m/"
                f"{allowed_wall.total_seconds() / 60:.0f}m)"
            )
        return None

    def total_checks(self, state: Mapping[str, Any]) -> int:
        """Checks this run has executed: the baseline plus every verifier check."""
        return len(state.get("baseline") or []) + len(state.get("checks") or [])

    def total_checks_allowed(self, state: Mapping[str, Any]) -> int:
        return self.recipe.verification.max_total_checks * self.budget(dict(state))

    def elapsed(self) -> timedelta:
        """Wall time since the run was first created, across restarts, minus
        the time it spent parked at a human gate -- otherwise a run resumed
        after a night's wait would breach `max_wall_time` on the spot."""
        record = self.store.get_run(self.run_id)
        if record and record.get("started_at"):
            from datetime import datetime

            try:
                started = datetime.fromisoformat(record["started_at"])
            except ValueError:
                return timedelta(seconds=time.monotonic() - self.started_monotonic)
            paused = timedelta(seconds=float(record.get("paused_s") or 0))
            return max(utcnow() - started - paused, timedelta(0))
        return timedelta(seconds=time.monotonic() - self.started_monotonic)

    def limits_note(self, state: dict[str, Any]) -> str:
        limits = self.recipe.limits
        multiplier = self.budget(state)
        return (
            f"{limits.max_iterations * multiplier} iterations allowed, "
            f"{limits.max_consiliums * multiplier} consilium(s) allowed, "
            f"{self.store.call_count(self.run_id)}/"
            f"{self.agent_call_limit(state) * multiplier} agent calls used, "
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

    def role_spec(self, name: str, state: Mapping[str, Any]) -> RoleSpec:
        """The RoleSpec to dispatch `name` with for this run.

        Tiered roles under `routing: live` resolve to the tier chain for the
        run's complexity; everything else (and shadow mode) is the role's own
        spec, so built-in recipes behave exactly as before until an operator
        flips routing.
        """
        return self.recipe.resolve_role(name, state.get("complexity"))

    def set_dispatch_tier(self, tier: str | None) -> None:
        self.store.update_run(self.run_id, dispatch_tier=tier)

    def set_complexity(self, level: str, source: str, reason: str) -> None:
        fields: dict[str, Any] = {"complexity": level}
        note = f"Complexity: {level} ({source}). {reason}"
        if source == "escalation":
            record = self.store.get_run(self.run_id) or {}
            fields["escalations"] = (record.get("escalations") or 0) + 1
            note = f"alloy: escalated {record.get('complexity')} -> {level} {reason}"
        self.store.update_run(self.run_id, **fields)
        if self.beads is not None:
            try:
                if source == "estimate":
                    self.beads.set_metadata(self.bead.id, {META_COMPLEXITY_ESTIMATED: level})
                self.beads.note(self.bead.id, note)
            except Exception:
                log.warning("could not record complexity on bead %s", self.bead.id, exc_info=True)

    def set_consiliums(self, count: int) -> None:
        self.store.update_run(self.run_id, consiliums=count)


def _accepts_on_spawn(runner: Any) -> bool:
    """Runners without a subprocess (HTTP adapters, test stubs) need not know
    about `on_spawn`; only pass it to those that declare the parameter."""
    return _accepts_kwarg(runner, "on_spawn")


def _accepts_kwarg(runner: Any, name: str) -> bool:
    """Only pass optional `run` keywords (`on_spawn`, `effort`,
    `resume_session`) to runners that declare them."""
    try:
        return name in inspect.signature(runner.run).parameters
    except (TypeError, ValueError):
        return False
