"""The `script` graph: a deterministic recipe with no agent calls.

    prepare_start -> prepare (one pair per command) -> check (one step per entry) -> finish
          \\-> human_gate (red)            \\-> human_gate (red twice, or wall time)

For work that needs no judgement -- a "ship" bead that bumps the version and
runs the full regression -- an LLM only adds cost and risk (it re-ran whole
suites and hit max_wall_time). The recipe YAML declares everything:

    name: ship
    graph: script
    prepare: [{cmd: "bash scripts/bump_client_version.sh", purpose: "bump client patch version"}]
    checks: [{cmd: "./scripts/run_tests.sh client-gates", purpose: "client gates", timeout_minutes: 15}]

Every command runs through `RunContext.run_check` (same logs, sandbox and
check cache as the agent recipes), each on its own: checks are never chained.
A red check gets one isolated rerun; green there is recorded as flaky and the
run goes on, red again parks the run for a human. A green run finishes `done`
and the engine commits the tree like any other recipe. `max_wall_time_minutes`
counts check time only: waiting for a queue or a human costs nothing.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from alloy.config import CommandSpec
from alloy.events import EVENT_FLAKY_CHECK
from alloy.models import CheckRequest, CheckResult, JudgeDecision, Outcome, clip
from alloy.recipes.role_prompts import log
from alloy.recipes.state import ScriptState
from alloy.recipes.workflow_nodes import _make_node_human_gate
from alloy.runtime import RunContext

OUTPUT_IN_REASON_CHARS = 1500


def _request(spec: CommandSpec, kind: str) -> CheckRequest:
    return CheckRequest(command=spec.cmd, purpose=spec.purpose, kind=kind, required=True)


def _timeout_s(spec: CommandSpec) -> float | None:
    return spec.timeout_minutes * 60 if spec.timeout_minutes is not None else None


def _label(spec: CommandSpec) -> str:
    return f"{spec.purpose} (`{spec.cmd}`)" if spec.purpose else f"`{spec.cmd}`"


def _park(
    stage: str, reason: str, question: str, *, limit_hit: str | None = None, resume_to: str | None = None
) -> dict[str, Any]:
    return {
        "stage": stage,
        "resume_to": resume_to or stage,
        "limit_hit": limit_hit,
        "decision": JudgeDecision(decision="human", reason=reason, next_instructions=question).model_dump(),
    }


def _passed(stage: str) -> dict[str, Any]:
    """A green step also clears human_gate's leftover "retry" decision, so the
    router only ever sees this step's own verdict."""
    return {"stage": stage, "decision": None}


def _failure(result: CheckResult) -> str:
    log_path = result.log_path or "(no log)"
    return f"{result.headline()}; log: {log_path}\n{clip(result.output_tail, OUTPUT_IN_REASON_CHARS)}".rstrip()


def check_seconds(state: ScriptState) -> float:
    """Time spent in `checks:` commands this run -- the only time the script
    graph charges to `max_wall_time_minutes`."""
    return sum(float(item.get("duration_s") or 0.0) for item in state.get("checks") or [])


def _make_node_prepare_start(ctx: RunContext):
    def prepare_start(state: ScriptState) -> dict[str, Any]:
        """Checkpoint "this prepare command is about to run" before it runs.
        Finding the marker already set means the command started but its
        result was never recorded (a crash in between): it may have run, so
        a human decides instead of a second version bump happening silently."""
        index = int(state.get("prepare_index", 0) or 0)
        spec = ctx.recipe.prepare[index]
        if state.get("prepare_started") == index:
            return _park(
                "prepare",
                f"prepare step {_label(spec)} started but its result was never recorded "
                "(the run stopped while it ran); it may have run already",
                "Check the tree. If the step had not run (or you undid it), resume and it runs once; "
                "if it had run, finish its effect by hand, then resume.",
                resume_to="prepare",
            )
        return {"prepare_started": index, "stage": "prepare", "decision": None}

    return prepare_start


def _make_node_prepare(ctx: RunContext):
    async def prepare(state: ScriptState) -> dict[str, Any]:
        index = int(state.get("prepare_index", 0) or 0)
        ctx.set_stage("prepare", iteration=state.get("iteration", 1))
        spec = ctx.recipe.prepare[index]
        result = await ctx.run_check(_request(spec, "build"), timeout_s=_timeout_s(spec))
        record = result.model_dump(mode="json")
        if not result.ok:
            return {
                "prepared": [record],
                "last_check": record,
                "prepare_started": None,
                **_park(
                    "prepare",
                    f"prepare step {_label(spec)} failed: {_failure(result)}",
                    "Fix what the prepare step needs, then resume; it runs again.",
                    resume_to="prepare_start",
                ),
            }
        return {
            "prepared": [record],
            "last_check": record,
            "prepare_index": index + 1,
            "prepare_started": None,
            **_passed("prepare"),
        }

    return prepare


def _make_node_check(ctx: RunContext):
    async def check(state: ScriptState) -> dict[str, Any]:
        index = int(state.get("check_index", 0) or 0)
        ctx.set_stage("check", iteration=state.get("iteration", 1))
        spec = ctx.recipe.checks[index]
        allowed_s = ctx.recipe.limits.max_wall_time_minutes * 60 * ctx.budget(dict(state))
        spent_s = check_seconds(state)
        if spent_s >= allowed_s:
            breach = f"max_wall_time reached ({spent_s / 60:.1f}m/{allowed_s / 60:.1f}m of check time)"
            return _park(
                "check",
                f"Alloy stopped the script before {_label(spec)}: {breach}",
                "Resume to grant one more wall-time window; the script continues with this check.",
                limit_hit=breach,
            )
        timeout_s = _timeout_s(spec)
        first = await ctx.run_check(_request(spec, "regression"), timeout_s=timeout_s)
        records = [first.model_dump(mode="json")]
        if first.ok:
            return {"checks": records, "last_check": records[-1], "check_index": index + 1, **_passed("check")}
        # One isolated rerun: a suite that is red once and green on its own is
        # flaky, not broken -- recorded, never silently swallowed.
        rerun = await ctx.run_check(_request(spec, "regression"), timeout_s=timeout_s)
        records.append(rerun.model_dump(mode="json"))
        if not rerun.ok:
            return {
                "checks": records,
                "last_check": records[-1],
                **_park(
                    "check",
                    f"check {_label(spec)} failed twice: {_failure(rerun)}",
                    "Fix the failure, then resume; this check runs again.",
                ),
            }
        _record_flaky(ctx, spec, first, rerun)
        return {
            "checks": records,
            "last_check": records[-1],
            "flaky": [spec.cmd],
            "check_index": index + 1,
            **_passed("check"),
        }

    return check


def _record_flaky(ctx: RunContext, spec: CommandSpec, first: CheckResult, rerun: CheckResult) -> None:
    note = (
        f"alloy: run {ctx.run_id} check {_label(spec)} was flaky: red once ({first.headline()}; "
        f"log {first.log_path}), green on its isolated rerun (log {rerun.log_path})"
    )
    log.warning("%s: %s", ctx.bead.id, note)
    ctx.store.events.emit(
        EVENT_FLAKY_CHECK,
        bead=ctx.bead.id,
        run=ctx.run_id,
        reason=f"{spec.purpose or spec.cmd}: red once, green on the isolated rerun",
        command=spec.cmd,
        logs=[first.log_path, rerun.log_path],
    )
    if ctx.beads is not None:
        try:
            ctx.beads.note(ctx.bead.id, note)
        except Exception:
            log.warning("could not record the flaky check on bead %s", ctx.bead.id, exc_info=True)


def _make_node_finish(ctx: RunContext):
    def finish(state: ScriptState) -> dict[str, Any]:
        ctx.set_stage("finished", iteration=state.get("iteration", 1))
        flaky = state.get("flaky") or []
        reason = f"script: {len(ctx.recipe.checks)} check(s) green"
        if flaky:
            reason += " (flaky, green on rerun: " + ", ".join(f"`{cmd}`" for cmd in flaky) + ")"
        return {"outcome": Outcome.DONE.value, "outcome_reason": reason, "stage": "finished"}

    return finish


def build_graph(ctx: RunContext):
    prepare_count = len(ctx.recipe.prepare)
    check_count = len(ctx.recipe.checks)

    def next_step(state: ScriptState) -> str:
        if (state.get("decision") or {}).get("decision") == "human":
            return "human_gate"
        if int(state.get("prepare_index", 0) or 0) < prepare_count:
            return "prepare_start"
        if int(state.get("check_index", 0) or 0) < check_count:
            return "check"
        return "finish"

    def after_human(state: ScriptState) -> str:
        return state.get("resume_target") or "check"

    graph = StateGraph(ScriptState)
    graph.add_node("prepare_start", _make_node_prepare_start(ctx))
    graph.add_node("prepare", _make_node_prepare(ctx))
    graph.add_node("check", _make_node_check(ctx))
    graph.add_node("human_gate", _make_node_human_gate(ctx))
    graph.add_node("finish", _make_node_finish(ctx))

    def after_prepare_start(state: ScriptState) -> str:
        return "human_gate" if (state.get("decision") or {}).get("decision") == "human" else "prepare"

    steps = ["prepare_start", "check", "finish", "human_gate"]
    graph.add_conditional_edges(START, next_step, steps)
    graph.add_conditional_edges("prepare_start", after_prepare_start, ["prepare", "human_gate"])
    graph.add_conditional_edges("prepare", next_step, steps)
    graph.add_conditional_edges("check", next_step, steps)
    graph.add_conditional_edges("human_gate", after_human, ["prepare_start", "prepare", "check"])
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=ctx.checkpointer)


def initial_state(ctx: RunContext) -> ScriptState:
    return ScriptState(
        bead_id=ctx.bead.id,
        run_id=ctx.run_id,
        title=ctx.bead.title,
        iteration=1,
        prepare_index=0,
        prepare_started=None,
        check_index=0,
        prepared=[],
        checks=[],
        last_check=None,
        flaky=[],
        instructions="",
        human_note="",
        budget_extensions=0,
        retries_on_tier=0,
        stage="starting",
        decision=None,
        outcome=None,
        outcome_reason="",
        limit_hit=None,
        limit_breach=None,
        resume_to=None,
        resume_target=None,
    )
