"""Shared TDD and landing verification loop."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Awaitable, Callable, NamedTuple

from alloy.config import RoleSpec
from alloy.models import (
    AcceptanceVerdict,
    AgentResult,
    Attempt,
    CheckResult,
    JudgeDecision,
    VerifierAction,
    clip,
    format_regression_areas,
    is_unavailable,
    parse_check_hints,
)
from alloy.prompts import assemble
from alloy.recipes.role_prompts import (
    _project_layer,
    _render_history,
    _render_results,
    _run_layer,
    _task_layer,
    acceptance_prompt,
    clip_diff,
    judge_prompt,
    log,
    verifier_prompt,
)
from alloy.recipes.state import TddState
from alloy.runtime import RunContext
from alloy.verify import (
    detect_commands,
    normalize_command,
    verifier_check_requests,
)
from alloy.worktree import is_test_path


def verifier_context(state: TddState, ctx: RunContext) -> dict[str, Any]:
    """Supply ordinary repository hints when a recipe omits context gathering."""
    context = state.get("context") or {}
    if context.get("check_hints"):
        return context
    hints = _check_hints(
        [],
        ctx.bead.check_hint,
        (ctx.worktree.path, ctx.worktrees.repo),
        memory_hints=parse_check_hints(state.get("memory_check_hints", "")),
    )
    return {**context, "check_hints": hints}


def _checks_of(state: TddState, iteration: int) -> list[dict[str, Any]]:
    return [item for item in state.get("checks") or [] if item.get("iteration") == iteration]


def _checks_headline(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return "(no checks run)"
    results = [CheckResult.model_validate(item) for item in checks]
    return "; ".join(f"[{result.kind}] {result.headline()}" for result in results)


def _evidence_packet(state: TddState, ctx: RunContext, diff: str) -> str:
    """A plain `str`: the packet is graph state (see CriticInput) and a layer
    of the critic and synthesize prompts, not a prompt of its own."""
    volatile = "\n\n".join(
        [
            f"## Current diff\n```diff\n{clip_diff(diff)}\n```",
            f"## Test results\n"
            f"{_render_results(_checks_of(state, state.get('iteration', 0)), state.get('verifier_stop'))}",
            f"## Attempt history\n{_render_history(state.get('attempts', [])) or '(none)'}",
        ]
    )
    regressions = format_regression_areas(
        state.get("memory_regressions") or {},
        (state.get("context") or {}).get("relevant_files") or [],
    )
    if regressions:
        volatile = f"{regressions}\n\n{volatile}"
    return str(
        assemble(
            "",
            _project_layer(state.get("memory_block", "")),
            _run_layer(state.get("context")),
            _task_layer(ctx.bead.task_brief(), ctx.bead.acceptance_criteria),
            volatile,
        ).text
    )


async def classify(
    ctx: RunContext,
    role: str,
    spec: RoleSpec,
    prompt: str,
    *,
    model_cls,
    default,
    iteration: int = 0,
):
    """Validate a classifier's answer, returning the supplied default on failure.

    The default's reason records the failure; its identity signals defaulting.
    """
    try:
        result = await ctx.call(
            role,
            spec,
            prompt,
            schema=model_cls.schema_for_agents(),
            iteration=iteration,
        )
    except Exception as exc:
        default.reason = f"{role} failed: {exc}"
        return default
    return answer_of(role, result, model_cls=model_cls, default=default)


def answer_of(role: str, result: AgentResult, *, model_cls, default):
    """`classify`'s validation step for a call made elsewhere."""
    try:
        if not result.ok:
            raise ValueError(result.error or result.summary)
        return model_cls.model_validate(result.structured)
    except Exception as exc:
        default.reason = f"{role} failed: {exc}"
        return default


def resumable_session(spec: RoleSpec, session: dict[str, Any] | None) -> str | None:
    """The tests writer's session id, if `spec`'s primary runner is the harness
    that opened it -- a cursor session cannot be resumed by claude."""
    if not session or not session.get("session_id"):
        return None
    if session.get("runner") != spec.runner:
        return None
    return str(session["session_id"])


def was_resumed(result: AgentResult) -> bool:
    return bool((result.usage or {}).get("resumed"))


async def call_in_session(
    ctx: RunContext,
    role: str,
    spec: RoleSpec,
    prompt: str,
    resumed_prompt: str,
    *,
    session_id: str | None,
    schema: dict[str, Any] | None = None,
    iteration: int = 0,
) -> AgentResult:
    """Call `role` inside the tests writer's session when `session_id` is set,
    else fresh with the full `prompt`.

    A resumed call that fails is retried once fresh, through the role's normal
    fallback chain; the ledger's usage_json `resumed` says which happened."""
    if session_id is not None:
        result = await ctx.call(
            role,
            replace(spec, fallback=None),
            resumed_prompt,
            schema=schema,
            iteration=iteration,
            resume_session=session_id,
        )
        if result.ok:
            return result
        log.warning(
            "%s: resuming session %s on %s failed (%s); running fresh",
            role,
            session_id,
            spec.label,
            (result.error or f"exit {result.exit_code}")[:200],
        )
    return await ctx.call(role, spec, prompt, schema=schema, iteration=iteration)


def _implementer_changed_tests(ctx: RunContext, state: TddState) -> list[str]:
    """Test files whose contents moved since prove_red: edited, deleted or
    added by the implementer, as opposed to written by the tests role."""
    before = state.get("test_fingerprints") or {}
    current = [path for path in ctx.worktrees.changed_files(ctx.worktree) if is_test_path(path)]
    paths = sorted(set(before) | set(current))
    after = ctx.worktrees.fingerprints(ctx.worktree, paths)
    return [path for path in paths if before.get(path) != after.get(path)]


class VerifyLoop(NamedTuple):
    """The nodes tdd-loop and land share -- the verifier's check loop, the
    acceptance gate and the judge -- plus their routing functions."""

    verifier_step: Callable[[TddState], Awaitable[dict[str, Any]]]
    route_after_verifier: Callable[[TddState], str]
    run_check_step: Callable[[TddState], Awaitable[dict[str, Any]]]
    route_after_check: Callable[[TddState], str]
    acceptance_gate: Callable[[TddState], Awaitable[dict[str, Any]]]
    route_after_acceptance: Callable[[TddState], str]
    judge: Callable[[TddState], Awaitable[dict[str, Any]]]


def _make_implementer(ctx, implementer_fallback):

    def _implementer(state: TddState) -> str:
        if state.get("implementer"):
            return str(state["implementer"])
        if implementer_fallback is not None:
            return implementer_fallback
        return ctx.role_spec("implement", state).runner

    return _implementer


def _make_verifier_nodes(ctx):

    async def verifier_step(state: TddState) -> dict[str, Any]:
        """Ask the verifier for one action: the next check to run, or stop.

        One agent call per node so LangGraph checkpoints between the proposal
        and its execution (`run_check_step`); a run killed mid-check resumes
        with every finished check already in the state. The verifier
        proposes; Alloy enforces the budgets here. A stop, or the
        per-iteration budget, hands the evidence to the acceptance gate; the
        run-wide cap routes to guard, which parks the run like
        max_iterations."""
        iteration = state.get("iteration", 0)
        ctx.set_stage("verify", iteration=iteration)
        spec = ctx.recipe.role("verifier")
        tests_session = state.get("tests_session")
        per_iteration = ctx.recipe.verification.max_checks_per_iteration
        allowed_total = ctx.total_checks_allowed(state)
        checks = list(state.get("checks") or [])
        iteration_checks = int(state.get("iteration_checks", 0) or 0)
        total = len(state.get("baseline") or []) + len(checks)
        update: dict[str, Any] = {
            "stage": "verify",
            "pending_check": None,
            "verify_route": "acceptance_gate",
            "verifier_stop": None,
        }

        if total >= allowed_total:
            update.update(
                verify_route="guard",
                decision=JudgeDecision(
                    decision="retry",
                    reason=f"the run's verification budget is spent ({total} checks)",
                ).model_dump(),
            )
            return update
        if iteration_checks >= per_iteration:
            # `pending_check` still holds the action behind the check that just ran.
            last_action = state.get("pending_check") or {}
            risks = list(last_action.get("remaining_risks") or [])
            stop = VerifierAction(
                action="stop",
                reason=f"Alloy stopped verification after {iteration_checks} checks this iteration",
                remaining_risks=[*risks, "verifier check budget exhausted"],
            )
            update["verifier_stop"] = stop.model_dump()
            return update

        failure = VerifierAction(action="stop")
        diff = ctx.diff()
        changed_before = ctx.worktrees.changed_files(ctx.worktree)
        prompt_args = (
            ctx.bead.task_brief(),
            ctx.bead.acceptance_criteria,
            verifier_context(state, ctx),
            diff,
            changed_before,
            checks,
            iteration,
            per_iteration - iteration_checks,
            allowed_total - total,
            state.get("attempts", []),
        )
        prompt_kwargs = dict(
            baseline_checks=state.get("baseline_checks", []),
            instructions=state.get("instructions", ""),
            memory=state.get("memory_block", ""),
            repo_root=ctx.worktree.path,
        )
        # The verifier continues the tests writer's session: the agent that
        # wrote the tests chooses how to verify them, context intact.
        session_id = resumable_session(spec, tests_session)
        try:
            result = await call_in_session(
                ctx,
                "verifier",
                spec,
                verifier_prompt(*prompt_args, **prompt_kwargs),
                verifier_prompt(*prompt_args, **prompt_kwargs, resumed=True),
                session_id=session_id,
                schema=VerifierAction.schema_for_agents(),
                iteration=iteration,
            )
        except Exception as exc:
            failure.reason = f"verifier failed: {exc}"
            result = None
        if session_id is not None and (result is None or not was_resumed(result)):
            # The session is gone (or the harness refused it): stop paying
            # for a failed resume before every fresh call.
            update["tests_session"] = None
        # The verifier is read-only by contract; an edit would let it pass
        # or fail the task for the wrong reason, so a human decides.
        changed_after = ctx.worktrees.changed_files(ctx.worktree)
        if changed_after != changed_before or ctx.diff() != diff:
            touched = sorted(set(changed_after) ^ set(changed_before)) or changed_after
            update.update(
                verify_route="human_gate",
                resume_to="implement",
                decision=JudgeDecision(
                    decision="human",
                    reason="verifier modified the worktree: " + ", ".join(touched),
                    next_instructions="Revert or keep the verifier's edits, then resume; the implementer runs again.",
                ).model_dump(),
            )
            return update
        action = failure if result is None else answer_of("verifier", result, model_cls=VerifierAction, default=failure)
        requests = verifier_check_requests(action) if action.action == "run" else []
        if action is failure or (action.action == "run" and not requests):
            why = failure.reason if action is failure else "verifier proposed a run without a command"
            stop = VerifierAction(
                action="stop",
                reason=f"verifier failed: {why}",
                remaining_risks=["verifier did not answer; no further checks were run"],
            )
            update["verifier_stop"] = stop.model_dump()
            return update
        if action.action == "stop":
            update["verifier_stop"] = action.model_dump()
            return update
        update.update(pending_check=action.model_dump(), verify_route="run_check_step")
        return update

    def route_after_verifier(state: TddState) -> str:
        return state.get("verify_route") or "acceptance_gate"

    return verifier_step, route_after_verifier


def _make_check_nodes(ctx, _implementer):

    async def run_check_step(state: TddState) -> dict[str, Any]:
        """Run the check the verifier proposed, as-is, and record the evidence.

        One command per node: the result is checkpointed before the verifier
        is asked again, so a crash here re-runs only this check. A red
        required check goes straight back to the implementer (through guard,
        so the iteration limits still apply) without a judge call; two
        unrunnable commands in a row ask a human."""
        iteration = state.get("iteration", 0)
        action = VerifierAction.model_validate(state.get("pending_check") or {})
        requests = verifier_check_requests(action)
        batch = len(requests) > 1
        records: list[dict[str, Any]] = []
        streak = int(state.get("unrunnable_streak", 0) or 0)
        failed_required: CheckResult | None = None

        for request in requests:
            result = await ctx.run_check(request)
            result.iteration = iteration
            records.append(result.model_dump(mode="json"))
            if not result.runnable:
                streak += 1
                if streak >= 2:
                    checks = [*(state.get("checks") or []), *records]
                    unrunnable = [CheckResult.model_validate(c) for c in checks[-2:]]
                    iteration_checks = int(state.get("iteration_checks", 0) or 0) + len(records)
                    return {
                        "stage": "verify",
                        "checks": records,
                        "last_check": records[-1],
                        "iteration_checks": iteration_checks,
                        "unrunnable_streak": streak,
                        "verify_route": "human_gate",
                        "resume_to": "implement",
                        "decision": JudgeDecision(
                            decision="human",
                            reason="the verifier proposed two commands in a row that could "
                            "not run: " + ", ".join(f"`{c.command}` ({c.headline()})" for c in unrunnable),
                            next_instructions="Check the worktree's toolchain or tell the "
                            "implementer what to verify, then resume; the implementer "
                            "runs again.",
                        ).model_dump(),
                    }
            else:
                streak = 0
            if result.required and not result.ok:
                failed_required = result

        checks = [*(state.get("checks") or []), *records]
        iteration_checks = int(state.get("iteration_checks", 0) or 0) + len(records)
        last = CheckResult.model_validate(records[-1]) if records else None
        ctx.set_tests_summary(f"{len(checks)} checks, last: {last.headline()}" if last else f"{len(checks)} checks")
        update: dict[str, Any] = {
            "stage": "verify",
            "checks": records,
            "last_check": records[-1] if records else None,
            "iteration_checks": iteration_checks,
            "verify_route": "verifier_step",
            "unrunnable_streak": streak,
        }

        if batch:
            # After a multi-check batch the verifier sees every result and
            # decides what to do next; the red-required shortcut below only
            # applies to single-check runs.
            return update

        if last is not None and not last.runnable:
            update["unrunnable_streak"] = streak
            return update

        if failed_required is not None:
            reason = f"required check `{failed_required.command}` failed ({failed_required.headline()})"
            update.update(
                verify_route="guard",
                decision=JudgeDecision(
                    decision="retry",
                    reason=reason,
                    next_instructions=(
                        f"The required check `{failed_required.command}` exited "
                        f"{failed_required.exit_code}; see '## Failed check'. Make it pass "
                        "without weakening or skipping it."
                    ),
                ).model_dump(),
                attempts=[
                    Attempt(
                        iteration=iteration,
                        implementer=_implementer(state),
                        change_summary=state.get("change_summary", ""),
                        checks=_checks_headline(_checks_of({"checks": checks}, iteration)),
                        decision="repair",
                        reason=reason,
                    ).model_dump()
                ],
            )
        return update

    def route_after_check(state: TddState) -> str:
        return state.get("verify_route") or "verifier_step"

    return run_check_step, route_after_check


def _normalize_acceptance(verdict, verification, iteration_checks):
    if verdict.decision == "accept" and verdict.confidence < verification.min_acceptance_confidence:
        verdict = AcceptanceVerdict(
            decision="escalate",
            reason=f"{verdict.reason}; confidence below threshold",
            confidence=verdict.confidence,
        )
    if verdict.decision == "verify_more" and iteration_checks >= verification.max_checks_per_iteration:
        verdict = AcceptanceVerdict(
            decision="escalate",
            reason=f"{verdict.reason}; verifier check budget exhausted",
            confidence=verdict.confidence,
        )
    return verdict


def route_after_acceptance(state: TddState) -> str:
    return state.get("acceptance_route") or "judge"


def _make_acceptance_nodes(ctx, _implementer):

    async def acceptance_gate(state: TddState) -> dict[str, Any]:
        """A cheap semantic check after the verifier stops: is the evidence enough?

        The acceptance role proposes accept / verify_more / repair / escalate;
        Alloy post-processes deterministically. Only escalate (including low
        confidence and a failed call) reaches the judge, and guard alone can
        finalise `done`."""
        iteration = state.get("iteration", 0)
        ctx.set_stage("acceptance", iteration=iteration)
        spec = ctx.recipe.role("acceptance")
        verification = ctx.recipe.verification
        iteration_checks = int(state.get("iteration_checks", 0) or 0)
        checks = _checks_of(state, iteration)
        stop_raw = state.get("verifier_stop")
        stop = VerifierAction.model_validate(stop_raw) if stop_raw else None
        changed_tests = _implementer_changed_tests(ctx, state)
        verdict = await classify(
            ctx,
            "acceptance",
            spec,
            acceptance_prompt(
                ctx.bead.acceptance_criteria,
                ctx.diff(),
                changed_tests,
                checks,
                stop_raw,
                declined=int(state.get("verify_more_declined", 0) or 0),
            ),
            model_cls=AcceptanceVerdict,
            default=AcceptanceVerdict(decision="escalate", reason="acceptance role failed"),
            iteration=iteration,
        )
        verdict = _normalize_acceptance(verdict, verification, iteration_checks)

        # The acceptance <-> verifier cycle never passes through guard, so the run's
        # own budgets and a stalled cycle (verify_more with no new evidence) are
        # enforced here.
        n_checks = len(state.get("checks") or [])
        declined = int(state.get("verify_more_declined", 0) or 0)
        if verdict.decision == "verify_more":
            breach = ctx.check_limits(state)
            if breach:
                return {
                    "stage": "acceptance",
                    "acceptance": verdict.model_dump(),
                    "acceptance_route": "guard",
                    "decision": JudgeDecision(
                        decision="retry",
                        reason=f"acceptance asked for more verification but {breach}",
                    ).model_dump(),
                }
            if state.get("verify_more_at_checks") == n_checks:
                verdict = AcceptanceVerdict(
                    decision="escalate",
                    reason=f"{verdict.reason}; verify_more produced no new evidence",
                    confidence=verdict.confidence,
                )

        update: dict[str, Any] = {
            "stage": "acceptance",
            "acceptance": verdict.model_dump(),
        }
        risks = "; ".join(stop.remaining_risks) if stop and stop.remaining_risks else ""
        row = dict(
            iteration=iteration,
            implementer=_implementer(state),
            change_summary=state.get("change_summary", ""),
            checks=_checks_headline(checks),
            changed_tests=changed_tests,
        )
        if verdict.decision == "accept":
            update.update(
                acceptance_route="guard",
                decision=JudgeDecision(
                    decision="done",
                    reason=f"acceptance gate: {verdict.reason}",
                    confidence=verdict.confidence,
                ).model_dump(),
            )
        elif verdict.decision == "repair":
            instructions = f"The acceptance gate asked for a repair: {verdict.reason}."
            if risks:
                instructions += f" Remaining risks: {risks}"
            reason = f"acceptance gate asked for a repair: {verdict.reason}"
            update.update(
                acceptance_route="guard",
                decision=JudgeDecision(
                    decision="retry",
                    reason=reason,
                    next_instructions=instructions,
                    confidence=verdict.confidence,
                ).model_dump(),
                attempts=[Attempt(**row, decision="repair", reason=reason).model_dump()],
            )
        elif verdict.decision == "verify_more":
            instructions = f"The acceptance gate found this unverified: {verdict.reason}"
            if risks:
                instructions += f" Remaining risks: {risks}"
            update.update(
                acceptance_route="verifier_step",
                verifier_stop=None,
                unrunnable_streak=0,
                verify_more_at_checks=n_checks,
                verify_more_declined=declined + 1,
                instructions=instructions,
            )
        else:
            update["acceptance_route"] = "judge"
        return update

    return acceptance_gate, route_after_acceptance


def _make_judge_node(ctx, _implementer):

    async def judge(state: TddState) -> dict[str, Any]:
        ctx.set_stage("judge", iteration=state.get("iteration", 0))
        spec = ctx.recipe.role("judge")
        diff = ctx.diff()
        changed_tests = _implementer_changed_tests(ctx, state)
        result = await ctx.call(
            "judge",
            spec,
            judge_prompt(
                ctx.bead.task_brief(),
                ctx.bead.acceptance_criteria,
                state.get("context", {}),
                diff,
                _checks_of(state, state.get("iteration", 0)),
                state.get("attempts", []),
                state.get("iteration", 0),
                ctx.limits_note(state),
                verifier_stop=state.get("verifier_stop"),
                changed_tests=changed_tests,
                memory=state.get("memory_block", ""),
            ),
            schema=JudgeDecision.schema_for_agents(),
            iteration=state.get("iteration", 0),
        )
        if is_unavailable(result):
            # The fallback chain is exhausted too: retrying would only burn
            # budget on a harness that cannot answer.
            return {
                "stage": "judge",
                "resume_to": "implement",
                "decision": _unavailable_decision("judge", result).model_dump(),
            }
        decision = _decision_from(result, state)
        attempt = Attempt(
            iteration=state.get("iteration", 0),
            implementer=_implementer(state),
            change_summary=state.get("change_summary", ""),
            checks=_checks_headline(_checks_of(state, state.get("iteration", 0))),
            decision=decision.decision,
            reason=decision.reason,
            changed_tests=changed_tests,
        )
        return {
            "decision": decision.model_dump(),
            "attempts": [attempt.model_dump()],
            "stage": "judge",
        }

    return judge


def make_verify_loop(ctx: RunContext, *, implementer_fallback: str | None = None) -> VerifyLoop:
    """Bind the shared verify/judge nodes to one run's context.

    `implementer_fallback` labels Attempt rows when the state names no
    implementer and the recipe has no implement role to ask (land)."""

    _implementer = _make_implementer(ctx, implementer_fallback)

    verifier_step, route_after_verifier = _make_verifier_nodes(ctx)

    run_check_step, route_after_check = _make_check_nodes(ctx, _implementer)

    acceptance_gate, route_after_acceptance = _make_acceptance_nodes(ctx, _implementer)

    judge = _make_judge_node(ctx, _implementer)

    return VerifyLoop(
        verifier_step=verifier_step,
        route_after_verifier=route_after_verifier,
        run_check_step=run_check_step,
        route_after_check=route_after_check,
        acceptance_gate=acceptance_gate,
        route_after_acceptance=route_after_acceptance,
        judge=judge,
    )


def _last_check_summary(state: TddState) -> dict[str, Any] | None:
    """The human gate's `last_check` field: the last verifier check once one
    has run, else the last baseline check; None before either."""
    check = state.get("last_check")
    if not check:
        baseline = state.get("baseline") or []
        check = baseline[-1] if baseline else None
    if not check:
        return None
    return {"command": check.get("command"), "exit_code": check.get("exit_code")}


def _is_red(check: dict[str, Any]) -> bool:
    """A runnable, required check that did not pass."""
    result = CheckResult.model_validate(check)
    return result.required and result.runnable and not result.ok


def _retry_at_iso(result) -> str | None:
    """The harness's own "available again at" time, for the human-gate payload."""
    retry_at = getattr(result, "retry_at", None)
    return retry_at.isoformat() if retry_at is not None else None


def _unavailable_decision(role: str, result) -> JudgeDecision:
    """Park at the human gate: every runner in the role's chain was unavailable."""
    return JudgeDecision(
        decision="human",
        reason=f"the {role} role could not run (primary and fallback unavailable): "
        f"{result.error or 'runner unavailable'}",
        next_instructions="Resume when a harness is available again.",
    )


def _decision_from(result, state: TddState) -> JudgeDecision:
    if result.structured:
        try:
            return JudgeDecision.model_validate(result.structured)
        except Exception:
            pass
    if not result.ok:
        return JudgeDecision(
            decision="retry",
            reason=f"judge runner failed: {result.error}",
            next_instructions="Judge was unavailable; continue from the test output.",
            retry_at=_retry_at_iso(result),
        )
    return JudgeDecision(
        decision="retry",
        reason="judge returned unparseable output; treating as retry",
        next_instructions=clip(result.text, 2000),
    )


def _check_hints(
    from_context: list[str],
    bead_hint: str | None,
    roots,
    memory_hints: list[str] | None = None,
) -> list[str]:
    """The operator's `alloy_test_cmd` hint comes first, then what the last
    DONE run's verifier could run (alloy:check-hints), then the context role's
    suggestions; autodetection from the project layout (the worktree, then the
    repository it was cut from) fills in only when the context role suggested
    nothing."""
    hints: list[str] = []
    for hint in [*(memory_hints or []), *from_context]:
        hint = normalize_command(hint) if hint else ""
        if hint and hint not in hints:
            hints.append(hint)
    if not any(from_context):
        for root in roots:
            for command in detect_commands(root):
                command = normalize_command(command)
                if command not in hints:
                    hints.append(command)
    if bead_hint:
        bead_hint = normalize_command(bead_hint)
        hints = [bead_hint] + [hint for hint in hints if hint != bead_hint]
    return hints
