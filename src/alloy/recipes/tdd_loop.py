"""The `tdd-loop` recipe.

    context -> estimate -> write failing tests -> implement -> verify -> judge -> guard
                                         ^                             |
                                         |                    done / retry /
                                         |                  consilium / human / abort
                                         +----- synthesize <- critics (parallel)

Two rules shape everything below:

* the graph carries summaries and artifact paths, never transcripts; and
* only `guard` decides whether the loop continues. The judge proposes; Alloy
  disposes. An LLM can never talk its way past a limit.
"""

from __future__ import annotations

import logging
import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from alloy.config import RoleSpec
from alloy.models import (
    AgentResult,
    Attempt,
    ComplexityEstimate,
    ContextPacket,
    Critique,
    JudgeDecision,
    Outcome,
    TestReport,
    clip,
    extract_bug_reports,
)
from alloy.runtime import RunContext
from alloy.verify import resolve_command

MAX_DIFF_CHARS = 12000
log = logging.getLogger(__name__)


def reset_or_extend(
    current: list[dict[str, Any]] | None, incoming: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Append critiques as they arrive; a None update clears the round."""
    if incoming is None:
        return []
    return [*(current or []), *incoming]


class TddState(TypedDict, total=False):
    bead_id: str
    run_id: str
    title: str

    context: dict[str, Any]
    complexity: str
    complexity_source: str
    test_command: str | None
    baseline: dict[str, Any] | None

    iteration: int
    consiliums: int
    instructions: str
    implementer: str  # the runner that actually produced the last change

    last_tests: dict[str, Any] | None
    decision: dict[str, Any] | None
    change_summary: str
    attempts: Annotated[list[dict[str, Any]], operator.add]
    reported_bugs: Annotated[list[dict[str, Any]], operator.add]
    critiques: Annotated[list[dict[str, Any]], reset_or_extend]
    budget_extensions: int

    stage: str
    outcome: str | None
    outcome_reason: str
    limit_hit: str | None
    human_note: str
    resume_to: str | None      # stage that parked the run at the human gate
    resume_target: str | None  # where human_gate sends the resumed run


class CriticInput(TypedDict):
    """Each critic sees the same evidence and nothing from its peers."""

    critic_index: int
    runner: str
    model: str | None
    evidence: str
    bead_id: str
    run_id: str
    iteration: int


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "relevant_files": {"type": "array", "items": {"type": "string"}},
        "test_command": {"type": "string"},
        "conventions": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "relevant_files", "test_command", "conventions", "risks"],
    "additionalProperties": False,
}

BUG_PROTOCOL = """If you discover a defect in existing code outside this task's scope (not the failing tests you were asked to make pass, and not your own change), do not fix it or silently work around it.
If the defect blocks your task, stop and report it.
If it does not block your task, finish your work and append the report.
Use one <bug>...</bug> block per defect, with each field on its own line:
title: short description; where: file:line; evidence: what you ran or saw.
blocks_task: yes|no (your opinion; Alloy decides)."""


def context_prompt(brief: str, acceptance: str) -> str:
    return f"""You are gathering context for another agent that will implement this task.
Read the repository. Do not modify any file.

{brief}

## Acceptance criteria
{acceptance or "(none stated)"}

Produce a context packet:
- summary: how this repo is laid out and where this change belongs (<= 300 words)
- relevant_files: paths the implementer will most likely touch or read
- test_command: the exact shell command this repo uses to run its test suite
- conventions: naming, structure and style rules an outsider would get wrong
- risks: things that could make this change break something else

{BUG_PROTOCOL}
"""


def estimate_prompt(brief: str, acceptance: str, context: dict[str, Any]) -> str:
    return f"""You are estimating how hard this task is
You are read-only: do not modify any file. Choose one complexity level:
- simple: one file, obvious change, tests are the spec
- medium: a few files or one new concept
- complex: cross-cutting, concurrency, new subsystem, ambiguous acceptance

{brief}

## Acceptance criteria
{acceptance or "(none stated)"}

## Repository context
{_render_context(context)}

Return complexity, reason and confidence in the required structured output.
"""


def tests_prompt(brief: str, acceptance: str, context: dict[str, Any]) -> str:
    return f"""Write failing tests for this task. Do not implement the behavior itself.

{brief}

## Acceptance criteria
{acceptance or "(none stated)"}

## Repository context
{_render_context(context)}

Requirements:
- Add tests that encode the acceptance criteria, following this repo's existing test conventions.
- The tests must fail right now, because the behavior does not exist yet.
- Do not write or modify implementation code. Tests only.
- Do not weaken or delete existing tests.
- Alloy owns task tracking, verification and git: do not run `bd`, do not commit,
  and do not run the whole test suite -- run only the tests you wrote.

When you are done, state in one paragraph which test files you added or changed and what each asserts.

{BUG_PROTOCOL}
"""


tests_prompt.__test__ = False  # This prompt helper may be imported by pytest modules.


def implement_prompt(
    brief: str,
    acceptance: str,
    context: dict[str, Any],
    test_command: str | None,
    instructions: str,
    history: list[dict[str, Any]],
    last_tests: dict[str, Any] | None,
) -> str:
    sections = [
        "Implement the smallest change that makes the failing tests pass.",
        brief,
        f"## Acceptance criteria\n{acceptance or '(none stated)'}",
        f"## Repository context\n{_render_context(context)}",
    ]
    if test_command:
        sections.append(f"## Test command\n`{test_command}`")
    if last_tests:
        sections.append(f"## Current test results\n{_render_tests(last_tests)}")
    if history:
        sections.append("## Previous attempts\n" + _render_history(history))
    if instructions:
        sections.append(f"## Required changes this iteration\n{instructions}")
    sections.append(
        "Rules:\n"
        "- Change implementation code, not the tests, unless a test is provably wrong "
        "about the stated acceptance criteria -- and say so explicitly if you do.\n"
        "- A bug in tests written for THIS task is in scope: use the tests provably "
        "wrong permission above, not a <bug> block.\n"
        "- Do not disable, skip or loosen assertions to get green.\n"
        "- Keep the change minimal and consistent with the repo's conventions.\n"
        "- Alloy owns task tracking, verification and git: do not run `bd`, do not "
        "commit, and do not run the whole test suite -- run the tests relevant to "
        "your change; Alloy runs the full suite when you finish.\n\n"
        "Finish with one paragraph describing what you changed and why, and say "
        "explicitly if you changed any test."
    )
    sections.append(BUG_PROTOCOL)
    return "\n\n".join(sections)


def judge_prompt(
    brief: str,
    acceptance: str,
    context: dict[str, Any],
    diff: str,
    tests: dict[str, Any] | None,
    history: list[dict[str, Any]],
    iteration: int,
    limits_note: str,
) -> str:
    return f"""You are judging whether a coding task is complete. You cannot edit code;
you only decide what happens next.

{brief}

## Acceptance criteria
{acceptance or "(none stated)"}

## Repository context
{_render_context(context)}

## Current diff
```diff
{clip(diff, MAX_DIFF_CHARS)}
```

## Test results
{_render_tests(tests)}

## Attempt history
{_render_history(history) or "(first attempt)"}

## Budget
iteration {iteration}; {limits_note}

Choose exactly one decision:
- "done"      -- tests pass and the diff genuinely satisfies the acceptance criteria
- "retry"     -- a specific, well-understood fix remains; put it in next_instructions
- "consilium" -- attempts are going in circles and independent opinions would help
- "human"     -- the task is ambiguous, or needs a decision or access Alloy does not have
- "abort"     -- the task cannot be completed as specified

Do not answer "done" if tests are failing. Do not answer "done" if the diff is empty.
Judge the diff on merit: green tests that were weakened or skipped are not done.
If the diff edits or deletes tests, say so in `reason` and decide whether the edit
was justified by the acceptance criteria.
"""


def critic_prompt(evidence: str) -> str:
    return f"""You are one of several independent critics reviewing a stuck coding task.
You are read-only: do not modify any file. You cannot see the other critics' opinions,
and that is deliberate -- give your own honest reading.

{evidence}

Identify the single most likely root cause of the failure and the concrete fix.
Be specific about files and behavior. If you believe the tests are wrong rather than
the implementation, say that explicitly.
"""


def synthesize_prompt(evidence: str, critiques: list[dict[str, Any]]) -> str:
    rendered = "\n\n".join(
        f"### Critic {index + 1} ({item.get('critic', 'unknown')}, "
        f"confidence {item.get('confidence', 0)})\n"
        f"root cause: {item.get('root_cause', '')}\n"
        f"evidence: {item.get('evidence', '')}\n"
        f"suggested fix: {item.get('suggested_fix', '')}"
        for index, item in enumerate(critiques)
    )
    return f"""Several independent critics reviewed a stuck coding task. Reconcile their
opinions into one instruction set for the implementer. You are read-only.

{evidence}

## Independent opinions
{rendered}

Where the critics agree, treat it as likely true. Where they disagree, decide which
reading the evidence actually supports and say why. Then write direct, concrete
instructions for the implementer: which files to change and what the change must do.
Output the instructions as prose, no preamble.
"""


# --------------------------------------------------------------------------
# rendering helpers -- these are what keep state small
# --------------------------------------------------------------------------


def _render_context(context: dict[str, Any] | None) -> str:
    if not context:
        return "(none gathered)"
    packet = ContextPacket.model_validate(context)
    lines = [packet.summary]
    if packet.relevant_files:
        lines.append("Relevant files: " + ", ".join(packet.relevant_files))
    if packet.conventions:
        lines.append("Conventions: " + "; ".join(packet.conventions))
    if packet.risks:
        lines.append("Risks: " + "; ".join(packet.risks))
    return "\n".join(line for line in lines if line)


def _render_tests(tests: dict[str, Any] | None) -> str:
    if not tests:
        return "(not run yet)"
    report = TestReport.model_validate(tests)
    return f"`{report.command}` -> exit {report.exit_code} ({report.headline()})\n\n{report.tail}"


def _render_history(history: list[dict[str, Any]]) -> str:
    return "\n".join(Attempt.model_validate(item).render() for item in history[-5:])


def _evidence_packet(state: TddState, ctx: RunContext, diff: str) -> str:
    return "\n\n".join(
        [
            ctx.bead.task_brief(),
            f"## Acceptance criteria\n{ctx.bead.acceptance_criteria or '(none stated)'}",
            f"## Repository context\n{_render_context(state.get('context'))}",
            f"## Current diff\n```diff\n{clip(diff, MAX_DIFF_CHARS)}\n```",
            f"## Test results\n{_render_tests(state.get('last_tests'))}",
            f"## Attempt history\n{_render_history(state.get('attempts', [])) or '(none)'}",
        ]
    )


# --------------------------------------------------------------------------
# graph
# --------------------------------------------------------------------------


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
            role, spec, prompt, schema=model_cls.schema_for_agents(), iteration=iteration
        )
        if not result.ok:
            raise ValueError(result.error or result.summary)
        return model_cls.model_validate(result.structured)
    except Exception as exc:
        default.reason = f"estimator failed: {exc}"
        return default


def build_graph(ctx: RunContext):
    """Compile the tdd-loop graph bound to one task's runtime."""

    def _capture_bugs(
        role: str, result: AgentResult, state: TddState, iteration: int
    ) -> list[dict[str, Any]]:
        titles = {bug["title"] for bug in state.get("reported_bugs", [])}
        reports = []
        for bug in extract_bug_reports(result.text):
            if bug.title in titles:
                continue
            bug.reporter = role
            bug.iteration = iteration
            reports.append(bug.model_dump())
            if ctx.beads is not None:
                try:
                    ctx.beads.note(
                        ctx.bead.id,
                        f"alloy: {role} reported bug '{bug.title}' at {bug.where} "
                        f"(blocks_task={bug.blocks_task})",
                    )
                except Exception:
                    log.warning("could not record bug on bead %s", ctx.bead.id, exc_info=True)
        return reports

    async def gather_context(state: TddState) -> dict[str, Any]:
        ctx.set_stage("context")
        spec = ctx.recipe.role("context")
        result = await ctx.call(
            "context",
            spec,
            context_prompt(ctx.bead.task_brief(), ctx.bead.acceptance_criteria),
            schema=CONTEXT_SCHEMA,
            iteration=0,
        )
        reported_bugs = _capture_bugs("context", result, state, 0)
        packet = _context_from(result)
        return {
            "reported_bugs": reported_bugs,
            "context": packet.compact(),
            "test_command": resolve_command(
                ctx.worktree.path,
                configured=ctx.bead.test_command or ctx.recipe.verify.command,
                from_context=packet.test_command,
            ),
            "stage": "context",
        }

    async def estimate(state: TddState) -> dict[str, Any]:
        ctx.set_stage("estimate")
        level = ctx.bead.complexity_override
        if level is not None:
            source = "override"
            reason = "operator override from alloy_complexity"
        else:
            default = ComplexityEstimate(complexity="medium")
            answer = await classify(
                ctx,
                "estimate",
                ctx.recipe.role("estimate"),
                estimate_prompt(
                    ctx.bead.task_brief(), ctx.bead.acceptance_criteria, state.get("context", {})
                ),
                model_cls=ComplexityEstimate,
                default=default,
            )
            level = answer.complexity
            source = "default" if answer is default else "estimate"
            reason = f"{answer.reason} (confidence {answer.confidence:.2f})"
        ctx.set_complexity(level, source, reason)
        return {"complexity": level, "complexity_source": source, "stage": "estimate"}

    async def write_tests(state: TddState) -> dict[str, Any]:
        ctx.set_stage("tests")
        spec = ctx.recipe.role("tests")
        result = await ctx.call(
            "tests",
            spec,
            tests_prompt(
                ctx.bead.task_brief(), ctx.bead.acceptance_criteria, state.get("context", {})
            ),
            iteration=0,
        )
        reported_bugs = _capture_bugs("tests", result, state, 0)
        if not result.ok:
            # Usually transient (a session limit, an outage): park the run at
            # the human gate with the tests stage as the resume target. Failing
            # the bead outright made a rate limit look like an impossible task.
            return {
                "reported_bugs": reported_bugs,
                "stage": "tests",
                "resume_to": "tests",
                "decision": JudgeDecision(
                    decision="human",
                    reason=f"the tests role failed: {result.error}",
                    next_instructions="Resume when the harness is available again; "
                    "the tests stage runs again from scratch.",
                ).model_dump(),
            }
        return {"stage": "tests", "instructions": "", "reported_bugs": reported_bugs}

    def route_after_tests(state: TddState) -> str:
        """A harness that cannot write the tests has nothing for the implementer
        to aim at: pause for a human, who decides whether to retry or cancel."""
        decision = (state.get("decision") or {}).get("decision")
        if decision == "abort":
            return "finish"
        if decision == "human":
            return "human_gate"
        return "baseline"

    def route_after_human(state: TddState) -> str:
        return state.get("resume_target") or "implement"

    async def baseline(state: TddState) -> dict[str, Any]:
        """Confirm the new tests actually fail before anyone implements anything."""
        ctx.set_stage("baseline")
        command = state.get("test_command")
        if not command:
            return {
                "baseline": None,
                "stage": "baseline",
                "instructions": "No test command could be determined; "
                "make sure the suite is runnable.",
            }
        report = await ctx.verify(command)
        note = ""
        if report.ok:
            note = (
                "Note: the suite already passes, so the new tests may not exercise the "
                "requested behavior. Check that they encode the acceptance criteria."
            )
        return {"baseline": report.model_dump(mode="json"), "stage": "baseline",
                "instructions": note}

    async def implement(state: TddState) -> dict[str, Any]:
        iteration = state.get("iteration", 0) + 1
        ctx.set_stage("implement", iteration=iteration)
        spec = ctx.recipe.role("implement")
        result = await ctx.call(
            "implement",
            spec,
            implement_prompt(
                ctx.bead.task_brief(),
                ctx.bead.acceptance_criteria,
                state.get("context", {}),
                state.get("test_command"),
                state.get("instructions", ""),
                state.get("attempts", []),
                state.get("last_tests"),
            ),
            iteration=iteration,
        )
        return {
            "reported_bugs": _capture_bugs("implement", result, state, iteration),
            "iteration": iteration,
            "stage": "implement",
            "instructions": "",
            "implementer": result.runner,
            "change_summary": result.summary,
        }

    async def verify(state: TddState) -> dict[str, Any]:
        ctx.set_stage("verify", iteration=state.get("iteration", 0))
        command = state.get("test_command")
        if not command:
            report = TestReport(
                command="(none)", exit_code=1,
                tail="Alloy could not determine a test command for this repository.",
            )
        else:
            report = await ctx.verify(command)
        ctx.set_tests_summary(report.headline())
        return {"last_tests": report.model_dump(mode="json"), "stage": "verify"}

    async def judge(state: TddState) -> dict[str, Any]:
        ctx.set_stage("judge", iteration=state.get("iteration", 0))
        spec = ctx.recipe.role("judge")
        diff = ctx.diff()
        result = await ctx.call(
            "judge",
            spec,
            judge_prompt(
                ctx.bead.task_brief(),
                ctx.bead.acceptance_criteria,
                state.get("context", {}),
                diff,
                state.get("last_tests"),
                state.get("attempts", []),
                state.get("iteration", 0),
                ctx.limits_note(state),
            ),
            schema=JudgeDecision.schema_for_agents(),
            iteration=state.get("iteration", 0),
        )
        decision = _decision_from(result, state)
        attempt = Attempt(
            iteration=state.get("iteration", 0),
            implementer=state.get("implementer") or ctx.recipe.role("implement").runner,
            change_summary=state.get("change_summary", ""),
            tests=TestReport.model_validate(state["last_tests"]).headline()
            if state.get("last_tests")
            else "(none)",
            decision=decision.decision,
            reason=decision.reason,
        )
        return {
            "decision": decision.model_dump(),
            "attempts": [attempt.model_dump()],
            "stage": "judge",
        }

    def guard(state: TddState) -> dict[str, Any]:
        """The deterministic gate.

        Every limit is enforced here and nowhere else, so there is exactly one
        place to read to know how the loop can end. The judge's decision is a
        proposal; what comes out of this function is what actually happens.
        """
        proposed = JudgeDecision.model_validate(
            state.get("decision") or {"decision": "retry", "reason": "no decision recorded"}
        )
        breach = ctx.check_limits(state)
        tests = state.get("last_tests")
        tests_green = tests is not None and TestReport.model_validate(tests).ok

        decision = proposed
        if proposed.decision == "done" and tests is not None and not tests_green:
            decision = JudgeDecision(
                decision="retry",
                reason="judge said done while tests are failing; overridden by Alloy",
                next_instructions=proposed.next_instructions or "Make the failing tests pass.",
                confidence=proposed.confidence,
            )

        if decision.decision in ("done", "abort", "human"):
            return {"stage": "guard", "decision": decision.model_dump()}

        if breach:
            return {
                "stage": "guard",
                "limit_hit": breach,
                "decision": JudgeDecision(
                    decision="human",
                    reason=f"Alloy stopped the loop: {breach}. "
                    f"Last judge reason: {decision.reason}",
                    next_instructions=decision.next_instructions,
                    confidence=decision.confidence,
                ).model_dump(),
            }

        if decision.decision == "consilium" and \
                state.get("consiliums", 0) >= ctx.recipe.limits.max_consiliums * ctx.budget(state):
            return {
                "stage": "guard",
                "instructions": decision.next_instructions
                or "Consilium budget is spent; fix the most likely cause directly.",
                "decision": JudgeDecision(
                    decision="retry",
                    reason="consilium budget exhausted; downgraded to retry",
                    next_instructions=decision.next_instructions,
                ).model_dump(),
            }

        if decision.decision == "consilium":
            return {"stage": "guard", "decision": decision.model_dump()}

        return {
            "stage": "guard",
            "instructions": decision.next_instructions,
            "decision": decision.model_dump(),
        }

    def route(state: TddState) -> str | list[Send]:
        decision = (state.get("decision") or {}).get("decision", "retry")
        if decision == "done":
            return "finish"
        if decision == "abort":
            return "finish"
        if decision == "human":
            return "human_gate"
        if decision == "consilium":
            return _dispatch_critics(state)
        return "implement"

    def _dispatch_critics(state: TddState) -> list[Send] | str:
        diff = ctx.diff()
        evidence = _evidence_packet(state, ctx, diff)
        critics = ctx.available_critics()
        if not critics:
            return "implement"
        return [
            Send(
                "critic",
                CriticInput(
                    critic_index=index,
                    runner=spec.runner,
                    model=spec.model,
                    evidence=evidence,
                    bead_id=state["bead_id"],
                    run_id=state["run_id"],
                    iteration=state.get("iteration", 0),
                ),
            )
            for index, spec in enumerate(critics)
        ]

    async def critic(payload: CriticInput) -> dict[str, Any]:
        ctx.set_stage("consilium", iteration=payload["iteration"])
        spec = ctx.critic_spec(payload["runner"], payload["model"])
        result = await ctx.call(
            f"critic:{payload['runner']}",
            spec,
            critic_prompt(payload["evidence"]),
            schema=Critique.schema_for_agents(),
            iteration=payload["iteration"],
        )
        critique = Critique(critic=payload["runner"], failed=not result.ok)
        if result.structured:
            critique = Critique.model_validate(
                {**result.structured, "critic": payload["runner"], "failed": False}
            )
        elif result.ok:
            critique.root_cause = clip(result.text, 1500)
        else:
            critique.root_cause = f"(critic unavailable: {result.error})"
        return {"critiques": [critique.model_dump()]}

    async def synthesize(state: TddState) -> dict[str, Any]:
        ctx.set_stage("synthesize", iteration=state.get("iteration", 0))
        usable = [item for item in state.get("critiques", []) if not item.get("failed")]
        if not usable:
            return {
                "consiliums": state.get("consiliums", 0) + 1,
                "critiques": None,
                "instructions": "Consilium produced no usable opinions; "
                "fix the most likely cause directly.",
                "stage": "synthesize",
            }
        ctx.set_consiliums(state.get("consiliums", 0) + 1)
        spec = ctx.recipe.consilium.synthesizer
        result = await ctx.call(
            "synthesize",
            spec,
            synthesize_prompt(_evidence_packet(state, ctx, ctx.diff()), usable),
            iteration=state.get("iteration", 0),
        )
        return {
            "consiliums": state.get("consiliums", 0) + 1,
            "critiques": None,
            "instructions": clip(result.text, 4000) if result.ok else usable[0]["suggested_fix"],
            "stage": "synthesize",
        }

    def human_gate(state: TddState) -> dict[str, Any]:
        """Persist and stop. Resuming delivers the human's instructions here."""
        decision = state.get("decision") or {}
        payload = interrupt(
            {
                "bead_id": state["bead_id"],
                "run_id": state["run_id"],
                "reason": decision.get("reason", ""),
                "question": decision.get("next_instructions", ""),
                "limit_hit": state.get("limit_hit"),
                "iteration": state.get("iteration", 0),
                "tests": (state.get("last_tests") or {}).get("exit_code"),
                "worktree": str(ctx.worktree.path),
            }
        )
        instructions = payload if isinstance(payload, str) else (payload or {}).get(
            "instructions", ""
        )
        return {
            "instructions": instructions or "Continue; the human provided no extra guidance.",
            "human_note": instructions or "",
            "limit_hit": None,
            "budget_extensions": state.get("budget_extensions", 0) + 1,
            "stage": "resumed",
            # Where to continue: the stage that parked us (e.g. "tests" after a
            # failed tests role), else the implementer.
            "resume_target": state.get("resume_to") or "implement",
            "resume_to": None,
            "decision": JudgeDecision(
                decision="retry", reason="resumed by human", next_instructions=instructions
            ).model_dump(),
        }

    def finish(state: TddState) -> dict[str, Any]:
        ctx.set_stage("finished", iteration=state.get("iteration", 0))
        decision = JudgeDecision.model_validate(
            state.get("decision") or {"decision": "abort", "reason": "no decision"}
        )
        outcome = Outcome.DONE if decision.decision == "done" else Outcome.FAILED
        return {
            "outcome": outcome.value,
            "outcome_reason": decision.reason,
            "stage": "finished",
        }

    graph = StateGraph(TddState)
    graph.add_node("context", gather_context)
    graph.add_node("estimate", estimate)
    graph.add_node("tests", write_tests)
    graph.add_node("baseline", baseline)
    graph.add_node("implement", implement)
    graph.add_node("verify", verify)
    graph.add_node("judge", judge)
    graph.add_node("guard", guard)
    graph.add_node("critic", critic, input_schema=CriticInput)
    graph.add_node("synthesize", synthesize)
    graph.add_node("human_gate", human_gate)
    graph.add_node("finish", finish)

    graph.add_edge(START, "context")
    graph.add_edge("context", "estimate")
    graph.add_edge("estimate", "tests")
    graph.add_conditional_edges("tests", route_after_tests, ["baseline", "finish", "human_gate"])
    graph.add_edge("baseline", "implement")
    graph.add_edge("implement", "verify")
    graph.add_edge("verify", "judge")
    graph.add_edge("judge", "guard")
    graph.add_conditional_edges(
        "guard", route, ["implement", "critic", "human_gate", "finish"]
    )
    graph.add_edge("critic", "synthesize")
    graph.add_edge("synthesize", "implement")
    graph.add_conditional_edges("human_gate", route_after_human, ["implement", "tests"])
    graph.add_edge("finish", END)

    return graph.compile(checkpointer=ctx.checkpointer)


def initial_state(ctx: RunContext) -> TddState:
    return TddState(
        bead_id=ctx.bead.id,
        run_id=ctx.run_id,
        title=ctx.bead.title,
        iteration=0,
        consiliums=0,
        instructions="",
        implementer="",
        attempts=[],
        reported_bugs=[],
        critiques=[],
        change_summary="",
        budget_extensions=0,
        stage="starting",
        outcome=None,
        outcome_reason="",
        limit_hit=None,
    )


def _context_from(result) -> ContextPacket:
    if result.structured:
        try:
            return ContextPacket.model_validate(result.structured)
        except Exception:
            pass
    return ContextPacket(summary=clip(result.text, 3000) if result.ok else
                         f"(context gathering failed: {result.error})")


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
        )
    return JudgeDecision(
        decision="retry",
        reason="judge returned unparseable output; treating as retry",
        next_instructions=clip(result.text, 2000),
    )
