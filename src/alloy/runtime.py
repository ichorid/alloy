"""Everything a recipe graph needs to touch the outside world.

Nodes stay declarative because all the I/O -- running a harness, running tests,
reading the diff, writing the ledger, enforcing limits -- lives here behind small
named methods.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from alloy.beads import META_COMPLEXITY_ESTIMATED, META_STAGE, Bead, BeadsClient
from alloy.config import RecipeConfig, RoleSpec, resolve_max_agent_calls, resolve_max_cheap_agent_calls
from alloy.limits import harness_for_runner, parse_retry_at
from alloy.limits.retry import parse_limit
from alloy.models import (
    UNAVAILABLE_KEY,
    AgentResult,
    CheckRequest,
    CheckResult,
    ProjectMemory,
    ProjectSnapshot,
    RunnerUnavailable,
    is_unavailable,
    utcnow,
)
from alloy.paths import project_brief
from alloy.runners import RunnerRegistry
from alloy.store import CHAIN_FREE_KEY, CHEAP_KEY, Store
from alloy.verify import noop_reason, run_check
from alloy.worktree import Worktree, WorktreeManager, tree_fingerprint

log = logging.getLogger("alloy.runtime")

LIMIT_REFUSED_KEY = "limit_refused"
"""usage_json flag on the synthetic result `RunContext.call` returns when it
refused to start a call because the run's wall time was already spent."""

PRECALL_EXEMPT_ROLES: frozenset[str] = frozenset({"harvest", "memory_reviewer"})
"""Roles that run after the loop has ended (lesson harvest, memory review):
the pre-call wall-time stop never refuses them."""

_WEAK_LIMIT_MAX_DURATION_S = 120.0
"""A failed call whose error only says "rate limit" (no harness limit
wording) trips the runner breaker only when it failed this fast."""


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
    checkout_note: str = ""
    """Set by the engine; see `task_brief`."""
    started_monotonic: float = field(default_factory=time.monotonic)
    _stage: str = "starting"
    _iteration: int = 0
    _budget_multiplier: int | None = None
    """The run's budget window (1 + human extensions) as last seen by
    `check_limits`; None until a node has consulted the limits in this
    process, in which case the pre-call wall-time check is skipped."""

    def task_brief(self) -> str:
        """The bead's brief as every role sees it, plus -- for an in-place run
        -- where the work lives: straight on the checked-out branch, with no
        separate bead branch for anything to be compared against."""
        brief = self.bead.task_brief()
        return f"{brief}\n\n## Checkout\n{self.checkout_note}" if self.checkout_note else brief

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
        # Runner circuit breaker: a harness that recently reported a usage,
        # spend or rate limit is skipped -- no ledger row, no budget -- and
        # the chain goes straight to its fallback. The last spec of the chain
        # always runs, as a probe, even when it is marked too.
        refused = self._refuse_call(role, spec)
        if refused is not None:
            return refused
        skipped_from = spec
        spec = self._skip_blocked(role, spec)
        if spec is not skipped_from and resume_session is not None:
            log.warning("%s: dropping resume session %s -- %s was skipped", role, resume_session, skipped_from.label)
            resume_session = None
        result = await self._call_one(
            role,
            spec,
            prompt,
            schema=schema,
            iteration=iteration,
            resume_session=resume_session,
        )
        # The primary and its fallbacks are one logical call: only the first
        # attempt that actually ran spends the run's agent-call budget.
        counted = not is_unavailable(result)
        while not result.ok and spec.fallback is not None:
            log.warning(
                "%s: %s failed (%s); falling back to %s",
                role,
                spec.label,
                (result.error or f"exit {result.exit_code}")[:200],
                spec.fallback.label,
            )
            if resume_session is not None:
                log.warning(
                    "%s: dropping resume session %s -- %s cannot resume a %s session",
                    role,
                    resume_session,
                    spec.fallback.label,
                    spec.label,
                )
                resume_session = None
            refused = self._refuse_call(role, spec.fallback)
            if refused is not None:
                # The primary already spent the window; a fallback would only
                # push the run further past its wall-time limit.
                return refused
            spec = self._skip_blocked(role, spec.fallback)
            result = await self._call_one(
                role,
                spec,
                prompt,
                schema=schema,
                iteration=iteration,
                resume_session=None,
                chain_free=counted,
            )
            counted = counted or not is_unavailable(result)
        return result

    def wall_time_breach(self) -> str | None:
        """The wall-time breach under the last budget window `check_limits`
        saw, or None (also when no window is known yet in this process)."""
        if self._budget_multiplier is None:
            return None
        limits = getattr(self.recipe, "limits", None)
        if limits is None:
            return None
        return self._wall_breach(limits.max_wall_time_minutes * self._budget_multiplier)

    def _wall_breach(self, allowed_minutes: float) -> str | None:
        elapsed = self.elapsed()
        allowed_wall = timedelta(minutes=allowed_minutes)
        if elapsed > allowed_wall:
            return (
                f"max_wall_time reached ({elapsed.total_seconds() / 60:.0f}m/{allowed_wall.total_seconds() / 60:.0f}m)"
            )
        return None

    def _refuse_call(self, role: str, spec: RoleSpec) -> AgentResult | None:
        """Wall time is checked before every agent call, not only between
        nodes: a call that would start after the run's wall-time budget is
        spent is not started. The synthetic failed result leaves no ledger
        row and spends no budget; the node's own limit gate (or guard) turns
        the breach into the usual needs-human / budget-landed stop."""
        if role in PRECALL_EXEMPT_ROLES:
            return None
        breach = self.wall_time_breach()
        if breach is None:
            return None
        log.warning("%s: not calling %s -- %s", role, spec.label, breach)
        now = utcnow()
        return AgentResult(
            runner=spec.runner,
            model=spec.model,
            ok=False,
            exit_code=-1,
            started_at=now,
            ended_at=now,
            duration_s=0.0,
            error=f"alloy: {breach}; the call was not started",
            usage={LIMIT_REFUSED_KEY: True, "limit": breach},
        )

    def _skip_blocked(self, role: str, spec: RoleSpec) -> RoleSpec:
        """The first spec of the chain starting at `spec` whose harness the
        breaker lets run; the chain's last spec when every one is marked (it
        runs as a probe). Skipped specs leave no ledger row and spend nothing."""
        while True:
            blocked_until = self._breaker_until(spec)
            if blocked_until is None:
                return spec
            if spec.fallback is None:
                log.warning(
                    "%s: %s is marked unavailable until %s but is the last runner in the chain; probing it",
                    role,
                    spec.label,
                    blocked_until.isoformat(),
                )
                return spec
            log.warning(
                "%s: skipping %s -- harness marked unavailable until %s; using %s",
                role,
                spec.label,
                blocked_until.isoformat(),
                spec.fallback.label,
            )
            spec = spec.fallback

    def _breaker_until(self, spec: RoleSpec) -> datetime | None:
        """When the breaker lets `spec`'s harness (or its model) run again;
        None when it may run now or belongs to no known harness."""
        harness = harness_for_runner(spec.runner)
        if harness is None:
            return None
        try:
            return self.store.runner_unavailable_until(harness, spec.model)
        except Exception:
            log.warning("runner breaker lookup failed for %s", spec.label, exc_info=True)
            return None

    def _update_breaker(self, spec: RoleSpec, result: AgentResult) -> None:
        """Mark the harness unavailable after a limit, clear it after a success."""
        harness = harness_for_runner(spec.runner)
        if harness is None:
            return
        try:
            if result.ok:
                self.store.clear_runner_unavailable(harness, spec.model)
                return
            hit = parse_limit(result.error or result.text, now=datetime.now())
            if hit is None or (not hit.strong and result.duration_s > _WEAK_LIMIT_MAX_DURATION_S):
                # A bare "rate limit" in the answer of a call that worked for
                # minutes is more likely the task's own subject than a limit.
                return
            until = hit.until(datetime.now()).astimezone()
            model = spec.model if hit.model_scoped else None
            self.store.mark_runner_unavailable(
                harness,
                until,
                model=model,
                reason=hit.reason,
                parsed=hit.reset_at is not None,
            )
            log.warning(
                "runner breaker: %s%s marked unavailable until %s (%s)",
                harness,
                f":{model}" if model else "",
                until.isoformat(),
                "reset time from the message" if hit.reset_at is not None else "no reset time given; default wait",
            )
        except Exception:
            log.warning("runner breaker update failed for %s", spec.label, exc_info=True)

    def _annotate(self, role: str, spec: RoleSpec, result: AgentResult, *, chain_free: bool) -> None:
        """Budget flags for the ledger row, and the runner breaker's verdict."""
        if chain_free:
            result.usage = {**result.usage, CHAIN_FREE_KEY: True}
        if self._is_cheap_role(role):
            result.usage = {**result.usage, CHEAP_KEY: True}
        self._update_breaker(spec, result)

    def _is_cheap_role(self, role: str) -> bool:
        limits = getattr(self.recipe, "limits", None)
        return bool(limits is not None and limits.is_cheap_role(role))

    async def _call_one(
        self,
        role: str,
        spec: RoleSpec,
        prompt: str,
        *,
        schema: dict[str, Any] | None,
        iteration: int,
        resume_session: str | None = None,
        chain_free: bool = False,
    ) -> AgentResult:
        """One harness invocation, visible in `inflight_calls` while it runs
        and moved into `agent_calls` in the same transaction when it ends."""
        log.info("%s: calling %s (iteration %d)", role, spec.label, iteration)
        call_id = uuid.uuid4().hex
        self.store.start_call(
            call_id,
            run_id=self.run_id,
            bead_id=self.bead.id,
            role=role,
            runner=spec.runner,
            model=spec.model,
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
                    runner=spec.runner,
                    model=spec.model,
                    ok=False,
                    exit_code=127,
                    started_at=now,
                    ended_at=now,
                    duration_s=0.0,
                    error=str(exc),
                )
            # Recorded in usage_json so `alloy logs` can show which calls
            # continued an earlier session.
            result.usage = {
                **(result.usage or {}),
                "resumed": resume_session is not None,
            }
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
            self._annotate(role, spec, result, chain_free=chain_free)
            self.store.finish_call(
                call_id,
                run_id=self.run_id,
                bead_id=self.bead.id,
                role=role,
                iteration=iteration,
                result=result,
            )
            finished = True
        finally:
            if not finished:
                self.store.discard_call(call_id)
        log.info(
            "%s: %s %s in %.0fs%s",
            role,
            result.runner,
            "ok" if result.ok else f"failed (exit {result.exit_code})",
            result.duration_s,
            f" -- {result.error[:200]}" if result.error else "",
        )
        return result

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
        """Run one check -- or, when this run already saw the exact same
        command pass against the same worktree fingerprint, return that
        passing result again (`cached=True`) without spending wall time.

        Only passes are reused, and only when the check itself left the tree
        as it found it; a failure always re-runs."""
        index = len(list(self.log_dir.glob("check-*.log"))) if self.log_dir.is_dir() else 0
        path = getattr(self.worktree, "path", None)
        before = await asyncio.to_thread(tree_fingerprint, path) if path is not None else ""
        if before and not noop_reason(request.command):
            cached = self._cached_check(request, before, index)
            if cached is not None:
                return cached
        result = await run_check(
            request,
            self.worktree.path,
            timeout_s=self.recipe.verification.max_command_timeout_minutes * 60,
            log_dir=self.log_dir,
            index=index,
        )
        after = await asyncio.to_thread(tree_fingerprint, path) if path is not None else ""
        result.fingerprint = after
        if result.ok and before and after == before:
            try:
                self.store.cache_check(self.run_id, request.command, before, result.model_dump(mode="json"))
            except Exception:
                log.warning("could not cache check %s", request.command, exc_info=True)
        return result

    def _cached_check(self, request: CheckRequest, fingerprint: str, index: int) -> CheckResult | None:
        try:
            raw = self.store.cached_check(self.run_id, request.command, fingerprint)
        except Exception:
            log.warning("check cache lookup failed for %s", request.command, exc_info=True)
            return None
        if raw is None:
            return None
        original = CheckResult.model_validate(raw)
        if not original.ok:
            return None
        log_path = None
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            target = self.log_dir / f"check-{index}-{request.kind}-{int(time.time() * 1000)}.log"
            target.write_text(
                f"$ {request.command}\npurpose={request.purpose}\nkind={request.kind}\nexit={original.exit_code}\n"
                f"cached=true\noriginal_log={original.log_path or ''}\n\n"
                "Not re-run: this exact command already passed in this run against an unchanged worktree.\n",
                encoding="utf-8",
            )
            log_path = str(target)
        log.info("check `%s`: reusing this run's passing result (worktree unchanged)", request.command)
        return original.model_copy(
            update={
                "purpose": request.purpose,
                "kind": request.kind,
                "required": request.required,
                "duration_s": 0.0,
                "log_path": log_path or original.log_path,
                "cached": True,
                "fingerprint": fingerprint,
            }
        )

    def diff(self, *, stat_only: bool = False) -> str:
        return self.worktrees.diff(self.worktree, stat_only=stat_only)

    # -- consilium --------------------------------------------------------

    def available_critics(self) -> list[RoleSpec]:
        """Critics whose CLI is actually installed. A missing harness is skipped,
        not fatal -- the consilium simply runs narrower."""
        return [spec for spec in self.recipe.consilium.critics if self.registry.available(spec.runner)]

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
        return resolve_max_agent_calls(self.recipe, state.get("complexity"), state.get("memory_calibration", ""))

    def cheap_call_limit(self, state: Mapping[str, Any]) -> int:
        """Tier-scaled ceiling on calls of `limits.cheap_roles`, before budget extensions."""
        return resolve_max_cheap_agent_calls(self.recipe, state.get("complexity"))

    def check_limits(self, state: Mapping[str, Any], *, include_iterations: bool = True) -> str | None:
        """Return a description of the first limit breached, else None.

        `include_iterations=False` is for gates *inside* an iteration (the
        verifier loop, triage, the acceptance gate): `max_iterations` caps how
        many implement iterations may *start*, so the last allowed iteration
        must still be verifiable. Every other limit applies everywhere."""
        limits = self.recipe.limits
        multiplier = self.budget(dict(state))
        self._budget_multiplier = multiplier

        iteration = int(state.get("iteration", 0) or 0)
        allowed_iterations = limits.max_iterations * multiplier
        if include_iterations and iteration >= allowed_iterations:
            return f"max_iterations reached ({iteration}/{allowed_iterations})"

        consiliums = int(state.get("consiliums", 0) or 0)
        if consiliums > limits.max_consiliums * multiplier:
            return f"max_consiliums reached ({consiliums}/{limits.max_consiliums * multiplier})"

        calls = self.store.call_count(self.run_id)
        allowed_calls = self.agent_call_limit(state) * multiplier
        if calls >= allowed_calls:
            return f"max_agent_calls reached ({calls}/{allowed_calls})"

        cheap_calls = self.store.call_count(self.run_id, cheap=True)
        allowed_cheap = self.cheap_call_limit(state) * multiplier
        if cheap_calls >= allowed_cheap:
            return f"max_cheap_agent_calls reached ({cheap_calls}/{allowed_cheap})"

        if self.total_checks(state) >= self.total_checks_allowed(state):
            return "max_total_checks reached"

        return self._wall_breach(limits.max_wall_time_minutes * multiplier)

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
            f"{self.store.call_count(self.run_id, cheap=True)}/"
            f"{self.cheap_call_limit(state) * multiplier} cheap-role calls used, "
            f"{self.elapsed().total_seconds() / 60:.0f}m of "
            f"{limits.max_wall_time_minutes * multiplier:.0f}m elapsed"
        )

    # -- progress reporting -----------------------------------------------

    def set_stage(self, stage: str, *, iteration: int | None = None) -> None:
        self._stage = stage
        if iteration is not None:
            self._iteration = iteration
        self.store.update_run(
            self.run_id,
            stage=stage,
            iteration=self._iteration,
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
                log.warning(
                    "could not record complexity on bead %s",
                    self.bead.id,
                    exc_info=True,
                )

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
