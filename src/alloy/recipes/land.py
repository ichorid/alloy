"""The `land` recipe.

    trial_merge(landing.target) -> verifier check loop -> acceptance gate -> judge -> guard

No context, tests or implement roles: the work is already on the bead branch.
The graph reuses tdd-loop's verify loop via `make_verify_loop`; only the trial
merge and the terminal guard are land-specific. Outcomes are plain state
strings, not `models.Outcome` members: `done` (the merged tree verified
green), `conflict` (trial merge aborted, conflicting files listed, worktree
unchanged), `red` (a required check failed on the merged tree, named in
`last_check`/`outcome_reason`). The primary checkout is never touched --
moving the target branch is Engine.land's job (alloy-vrh.8).
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from alloy.models import JudgeDecision
from alloy.recipes import tdd_loop
from alloy.recipes.shared_verification import _checks_of, _is_red, make_verify_loop
from alloy.recipes.state import TddState
from alloy.runtime import RunContext


def build_graph(ctx: RunContext):
    """Compile the land graph bound to one landing's runtime."""
    verify = make_verify_loop(ctx, implementer_fallback="land")

    async def trial_merge(state: TddState) -> dict[str, Any]:
        """Merge `landing.target` into the bead branch inside its worktree.

        On conflict the merge is aborted (WorktreeManager.trial_merge leaves
        the tree exactly where it was) and the run ends `conflict` with the
        files listed; on success the merged tree goes to the verifier."""
        target = ctx.recipe.landing.target
        ctx.set_stage(f"trial-merge:{target}")
        result = ctx.worktrees.trial_merge(ctx.worktree, target)
        if result.ok:
            return {"stage": "trial-merge"}
        return {
            "stage": "trial-merge",
            "outcome": "conflict",
            "conflict_files": result.conflict_files,
            "outcome_reason": (
                f"trial merge of '{target}' into {ctx.worktree.branch} conflicted on: "
                + ", ".join(result.conflict_files)
            ),
        }

    def route_after_trial_merge(state: TddState) -> str:
        return END if state.get("outcome") == "conflict" else "verifier_step"

    def land_guard(state: TddState) -> dict[str, Any]:
        """The terminal gate: `done` only on a green, accepted merged tree.

        Land has no implementer, so anything short of done -- a red required
        check, a repair or retry proposal, a spent check budget, a verifier
        that touched the tree -- ends the run `red` instead of looping."""
        ctx.set_stage("guard", iteration=state.get("iteration", 0))
        proposed = JudgeDecision.model_validate(
            state.get("decision") or {"decision": "retry", "reason": "no decision recorded"}
        )
        tests_green = not any(_is_red(check) for check in _checks_of(state, state.get("iteration", 0)))
        if proposed.decision == "done" and tests_green:
            return {
                "stage": "guard",
                "decision": proposed.model_dump(),
                "outcome": "done",
                "outcome_reason": proposed.reason,
            }
        reason = proposed.reason or "verification did not pass on the merged tree"
        last = state.get("last_check") or {}
        if not tests_green and last.get("command"):
            reason = f"required check `{last['command']}` failed on the merged tree"
        return {
            "stage": "guard",
            "decision": proposed.model_dump(),
            "outcome": "red",
            "outcome_reason": reason,
        }

    def _guard_instead_of_human(route) -> Any:
        """Land cannot park at a human gate mid-graph: a verifier that edited
        the worktree or proposed unrunnable commands ends the run red, and the
        caller (engine/scheduler) decides what happens next."""

        def routing(state: TddState) -> str:
            target = route(state)
            return "guard" if target == "human_gate" else target

        return routing

    graph = StateGraph(TddState)
    graph.add_node("trial_merge", trial_merge)
    graph.add_node("verifier_step", verify.verifier_step)
    graph.add_node("run_check_step", verify.run_check_step)
    graph.add_node("acceptance_gate", verify.acceptance_gate)
    graph.add_node("judge", verify.judge)
    graph.add_node("guard", land_guard)

    graph.add_edge(START, "trial_merge")
    graph.add_conditional_edges("trial_merge", route_after_trial_merge, ["verifier_step", END])
    graph.add_conditional_edges(
        "verifier_step",
        _guard_instead_of_human(verify.route_after_verifier),
        ["run_check_step", "acceptance_gate", "guard"],
    )
    graph.add_conditional_edges(
        "run_check_step",
        _guard_instead_of_human(verify.route_after_check),
        ["verifier_step", "guard"],
    )
    graph.add_conditional_edges(
        "acceptance_gate",
        verify.route_after_acceptance,
        ["guard", "verifier_step", "judge"],
    )
    graph.add_edge("judge", "guard")
    graph.add_edge("guard", END)

    return graph.compile(checkpointer=ctx.checkpointer)


def initial_state(ctx: RunContext) -> TddState:
    """Same run-start state as tdd-loop (memory snapshot included); the land
    graph simply never populates the tests/implement fields."""
    return tdd_loop.initial_state(ctx)
