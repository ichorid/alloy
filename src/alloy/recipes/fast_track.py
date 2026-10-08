"""The `fast-track` recipe: for simple beads, or beads TDD fits poorly.

    implement -> run_check -> implement -> ... -> guard -> start_final_pass -> run_check -> ...
           \\-> guard (done/retry/human, nothing to check)        \\-> human_gate -> implement

One role (`implement`) proposes the change, proposes its own real verification
commands, and judges its own done/retry/human -- there is no separate tests,
verifier, acceptance, judge or consilium role. Alloy's only server-side rule:
a `done` claim is rejected unless at least one real check ran this iteration
(see `_make_node_fast_implement`); this is the whole of "minimal, not zero,
verification".

`guard`, `start_final_pass`, `human_gate`, `finish` and `harvest` are the exact
same closures tdd_loop.py uses -- reused verbatim because this graph's nodes
are named the same way (`implement`) and the state shape is a deliberate
subset of TddState (see `state.FastTrackState`).
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from alloy.models import (
    CHECK_HINTS_KEY,
    CheckRequest,
    FastTrackVerdict,
    JudgeDecision,
    ProjectMemory,
    is_unavailable,
)
from alloy.recipes.role_prompts import fast_track_prompt
from alloy.recipes.shared_verification import (
    _checks_of,
    _unavailable_decision,
    answer_of,
    call_in_session,
    limit_gate,
    limit_refused,
    limit_stop,
    resumable_session,
)
from alloy.recipes.role_prompts import extract_summary
from alloy.recipes.state import FastTrackState
from alloy.recipes.tdd_loop import existing_lessons, flag_stale_embed_block
from alloy.recipes.workflow_nodes import (
    _make_node__capture_bugs,
    _make_node_finish,
    _make_node_guard,
    _make_node_harvest,
    _make_node_human_gate,
    _make_node_remember_calibration,
    _make_node_remember_check_hints,
    _make_node_remember_lesson,
    _make_node_route,
    _make_node_route_after_human,
    _make_node_start_final_pass,
    _make_node__dispatch_critics,
)
from alloy.runtime import RunContext


def _make_node_fast_implement(ctx: RunContext, _capture_bugs):
    async def implement(state: FastTrackState) -> dict[str, Any]:
        previous_iteration = state.get("iteration", 0)
        iteration = previous_iteration + 1
        # implement <-> run_check loops without guard, so the limits are
        # checked before every implement call. Coming straight back from a
        # green run_check the worker is still verifying the same change (its
        # "done" needs a check of this iteration), so that edge is bounded by
        # agent calls, total checks and wall time, not by max_iterations --
        # otherwise a run would park on the very call that says done.
        from_check = state.get("verify_route") == "implement"
        breach = limit_gate(ctx, state, include_iterations=not from_check)
        if breach:
            ctx.set_stage("implement", iteration=previous_iteration)
            return {
                "stage": "implement",
                "verify_route": "guard",
                **limit_stop(
                    breach,
                    f"before implement iteration {iteration}",
                    next_instructions=state.get("instructions", ""),
                ),
            }
        ctx.set_stage("implement", iteration=iteration)
        spec = ctx.role_spec("implement", state)
        worker_session = state.get("worker_session")
        session_id = resumable_session(spec, worker_session)
        prompt_args = (ctx.task_brief(), ctx.bead.acceptance_criteria, state.get("instructions", ""))
        prompt_kwargs = dict(
            history=state.get("attempts", []),
            last_checks=_checks_of(state, previous_iteration) if session_id else [],
            diff=ctx.diff(),
            memory=state.get("memory_block", ""),
        )
        result = await call_in_session(
            ctx,
            "implement",
            spec,
            fast_track_prompt(*prompt_args, **prompt_kwargs),
            fast_track_prompt(*prompt_args, **prompt_kwargs),
            session_id=session_id,
            schema=FastTrackVerdict.schema_for_agents(),
            iteration=iteration,
        )
        reported_bugs = _capture_bugs("implement", result, state, iteration)
        if limit_refused(result):
            return {
                "reported_bugs": reported_bugs,
                "iteration": iteration,
                "stage": "implement",
                "verify_route": "guard",
                **limit_stop(result.usage.get("limit") or result.error, f"during implement iteration {iteration}"),
            }
        if is_unavailable(result):
            return {
                "reported_bugs": reported_bugs,
                "iteration": iteration,
                "stage": "implement",
                "resume_to": "implement",
                "decision": _unavailable_decision("implement", result).model_dump(),
            }
        default = FastTrackVerdict(action="retry", reason="malformed or failed answer")
        verdict = answer_of("implement", result, model_cls=FastTrackVerdict, default=default)
        ran_checks_this_iteration = bool(_checks_of(state, previous_iteration))
        if verdict.action == "done" and not ran_checks_this_iteration:
            # Alloy enforces "minimal, not zero, verification": a done claim
            # with no check that has actually run this iteration is never
            # honored directly -- whether the model proposed no checks at all,
            # or proposed some but declared done before they ran.
            if verdict.checks:
                verdict = FastTrackVerdict(
                    action="run_check",
                    checks=verdict.checks,
                    reason=verdict.reason,
                    next_instructions=verdict.next_instructions,
                    confidence=verdict.confidence,
                )
            else:
                verdict = FastTrackVerdict(
                    action="retry",
                    reason="done claimed with no check run this iteration; Alloy requires at least one",
                    next_instructions="Propose and run at least one real verification command "
                    "(action=run_check) before claiming done.",
                )
        update: dict[str, Any] = {
            "limit_breach": None,
            "reported_bugs": reported_bugs,
            "implementer_stopped": any(bug.get("blocks_task") for bug in reported_bugs),
            "iteration": iteration,
            "worker_session": {"runner": result.runner, "session_id": result.session_id} if result.session_id else None,
            "last_instructions": state.get("instructions", ""),
            "implementer": result.runner,
            "change_summary": extract_summary(result.text) if result.ok else result.summary,
            "stage": "implement",
            "instructions": verdict.next_instructions,
        }
        if verdict.action == "run_check":
            update.update(pending_checks=[c.model_dump() for c in verdict.checks], verify_route="run_check")
        else:
            decision_by_action = {"done": "done", "retry": "retry", "human": "human"}
            update.update(
                verify_route="guard",
                decision=JudgeDecision(
                    decision=decision_by_action[verdict.action],
                    reason=verdict.reason,
                    next_instructions=verdict.next_instructions,
                    confidence=verdict.confidence,
                ).model_dump(),
            )
            if verdict.action == "human":
                update["resume_to"] = "implement"
        return update

    return implement


def _make_node_fast_run_check(ctx: RunContext):
    async def run_check(state: FastTrackState) -> dict[str, Any]:
        iteration = state.get("iteration", 0)
        requests = [CheckRequest.model_validate(c) for c in state.get("pending_checks") or []]
        records: list[dict[str, Any]] = []
        streak = int(state.get("unrunnable_streak", 0) or 0)
        failed_required = None
        for request in requests:
            result = await ctx.run_check(request)
            result.iteration = iteration
            records.append(result.model_dump(mode="json"))
            if not result.runnable:
                streak += 1
                if streak >= 2:
                    return {
                        "checks": records,
                        "last_check": records[-1],
                        "unrunnable_streak": streak,
                        "iteration_checks": int(state.get("iteration_checks", 0) or 0) + len(records),
                        "pending_checks": [],
                        "verify_route": "human_gate",
                        "resume_to": "implement",
                        "decision": JudgeDecision(
                            decision="human",
                            reason="two unrunnable verification commands in a row",
                            next_instructions="Check the worktree's toolchain, then resume; implement runs again.",
                        ).model_dump(),
                    }
            else:
                streak = 0
            if request.required and not result.ok:
                failed_required = result
        update: dict[str, Any] = {
            "checks": records,
            "last_check": records[-1] if records else None,
            "unrunnable_streak": streak,
            "iteration_checks": int(state.get("iteration_checks", 0) or 0) + len(records),
            "pending_checks": [],
        }
        if failed_required is not None:
            reason = f"required check `{failed_required.command}` failed ({failed_required.headline()})"
            update.update(
                verify_route="guard",
                decision=JudgeDecision(
                    decision="retry",
                    reason=reason,
                    next_instructions=(
                        f"The required check `{failed_required.command}` exited "
                        f"{failed_required.exit_code}. Fix it without weakening or skipping it."
                    ),
                ).model_dump(),
            )
        else:
            update["verify_route"] = "implement"
        return update

    def route_after_fast_check(state: FastTrackState) -> str:
        return state.get("verify_route") or "implement"

    return run_check, route_after_fast_check


def route_after_fast_implement(state: FastTrackState) -> str:
    return state.get("verify_route") or "guard"


def build_graph(ctx: RunContext):
    _capture_bugs = _make_node__capture_bugs(ctx)
    implement = _make_node_fast_implement(ctx, _capture_bugs)
    run_check, route_after_fast_check = _make_node_fast_run_check(ctx)
    start_final_pass = _make_node_start_final_pass(ctx)
    guard = _make_node_guard(ctx)
    _dispatch_critics = _make_node__dispatch_critics(ctx)
    route = _make_node_route(_dispatch_critics)
    human_gate = _make_node_human_gate(ctx)
    route_after_human = _make_node_route_after_human()
    remember_check_hints = _make_node_remember_check_hints(ctx)
    remember_calibration = _make_node_remember_calibration(ctx)
    remember_lesson = _make_node_remember_lesson(ctx)
    finish = _make_node_finish(ctx, remember_check_hints, remember_calibration)
    harvest = _make_node_harvest(ctx, remember_lesson)

    graph = StateGraph(FastTrackState)
    graph.add_node("implement", implement)
    graph.add_node("run_check", run_check)
    graph.add_node("guard", guard)
    graph.add_node("start_final_pass", start_final_pass)
    graph.add_node("human_gate", human_gate)
    graph.add_node("finish", finish)
    graph.add_node("harvest", harvest)

    graph.add_edge(START, "implement")
    graph.add_conditional_edges("implement", route_after_fast_implement, ["run_check", "guard", "human_gate"])
    graph.add_conditional_edges("run_check", route_after_fast_check, ["implement", "guard", "human_gate"])
    # start_final_pass only sets `instructions`; it never proposes a check
    # itself (unlike tdd_loop's verifier_step, there is no separate role to ask
    # here). `implement` is the only node that can propose a fresh check, so
    # the final pass re-enters there, not at run_check.
    graph.add_edge("start_final_pass", "implement")
    # `route`'s "critic"/"consilium" branch is unreachable here: this recipe
    # has no consilium.critics, so _dispatch_critics always returns "implement".
    graph.add_conditional_edges("guard", route, ["implement", "human_gate", "finish", "start_final_pass"])
    graph.add_conditional_edges("human_gate", route_after_human, ["implement"])
    graph.add_edge("finish", "harvest")
    graph.add_edge("harvest", END)

    return graph.compile(checkpointer=ctx.checkpointer)


def initial_state(ctx: RunContext) -> FastTrackState:
    memory: ProjectMemory | None = ctx.project_memory()
    flag_stale_embed_block(ctx, memory)
    return FastTrackState(
        bead_id=ctx.bead.id,
        run_id=ctx.run_id,
        title=ctx.bead.title,
        memory_block=memory.render() if memory is not None else "",
        memory_check_hints=memory.body_of(CHECK_HINTS_KEY) if memory is not None else "",
        memory_pinned_checks="",
        memory_calibration="",
        memory_keys=sorted(memory.entries) if memory is not None else [],
        memory_lessons=existing_lessons(memory),
        memory_regressions={},
        iteration=0,
        consiliums=0,
        retries_on_tier=0,
        escalations=[],
        instructions="",
        implementer="",
        worker_session=None,
        pending_checks=[],
        checks=[],
        iteration_checks=0,
        unrunnable_streak=0,
        last_check=None,
        last_instructions="",
        verify_route=None,
        attempts=[],
        reported_bugs=[],
        implementer_stopped=False,
        change_summary="",
        budget_extensions=0,
        journal=[],
        final_pass=False,
        stage="starting",
        outcome=None,
        outcome_reason="",
        limit_hit=None,
        limit_breach=None,
    )
