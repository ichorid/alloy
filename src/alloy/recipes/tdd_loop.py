"""The `tdd-loop` recipe.

    context -> estimate -> write failing tests -> implement -> verification loop -> acceptance gate -> guard
                                         ^              |        ^        |            |  escalate ^
                                         |          <bug> reports |  red required     |  -> judge --+
                                         |              v        |  check -> guard  repair -> guard; verify_more -> loop
                                         |            triage ----+
                                         |         (blocking -> remediate; needs-human -> human gate)
                                         +----- synthesize <- critics (parallel)

Two rules shape everything below:

* the graph carries summaries and artifact paths, never transcripts; and
* only `guard` decides whether the loop continues. The judge proposes; Alloy
  disposes. An LLM can never talk its way past a limit.
"""

from __future__ import annotations

import logging
import operator
import re
from dataclasses import replace
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from alloy.beads import LABEL_BUG, LABEL_HUMAN, META_DISCOVERED_IN_RUN, META_RECIPE
from alloy.config import RoleSpec
from alloy.models import (
    CHECK_HINTS_KEY,
    LESSON_KEY_PREFIX,
    AcceptanceVerdict,
    AgentResult,
    Attempt,
    BugReport,
    BugTriage,
    CheckRequest,
    CheckResult,
    ComplexityEstimate,
    ContextPacket,
    Critique,
    HarvestAnswer,
    JudgeDecision,
    Outcome,
    utcnow,
    ProjectMemory,
    ProjectSnapshot,
    ScopeVerdict,
    TestsOutput,
    VerifierAction,
    clip,
    extract_bug_reports,
    format_check_hints,
    next_level,
    parse_check_hints,
    with_provenance,
)
from alloy.diffs import clip_diff_per_file
from alloy.prompts import assemble
from alloy.runtime import RunContext
from alloy.verify import detect_commands, normalize_command
from alloy.worktree import is_test_path

MAX_DIFF_CHARS = 12000
PER_FILE_DIFF_CHARS = 4000
"""Per-file budget inside MAX_DIFF_CHARS so one generated file cannot hide the rest."""
PROJECT_CONTEXT_CHARS = 6000
"""Hard cap on the project context packet handed to the scope and triage roles."""
log = logging.getLogger(__name__)


def clip_diff(diff: str) -> str:
    """The diff as every prompt sees it: per-file clipped, then capped overall."""
    return clip_diff_per_file(diff, per_file=PER_FILE_DIFF_CHARS, total=MAX_DIFF_CHARS)


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
    memory_block: str                          # rendered project memory, fixed at run start
    memory_check_hints: str                    # stored alloy:check-hints body, fixed at run start
    complexity: str
    complexity_source: str
    retries_on_tier: int
    escalations: Annotated[list[dict[str, Any]], operator.add]
    baseline_checks: list[dict[str, Any]]      # CheckRequest dicts from the tests role
    baseline: list[dict[str, Any]] | None      # CheckResult dicts from prove_red
    baseline_repairs: int
    tests_session: dict[str, Any] | None       # {runner, session_id} of the last tests call
    test_fingerprints: dict[str, str]          # path -> sha256 of every test file after prove_red

    iteration: int
    consiliums: int
    instructions: str
    implementer: str  # the runner that actually produced the last change

    checks: Annotated[list[dict[str, Any]], operator.add]  # CheckResult dicts, every iteration
    iteration_checks: int                      # checks run in the current iteration
    verifier_stop: dict[str, Any] | None       # the VerifierAction that ended the last loop
    last_check: dict[str, Any] | None          # the most recent CheckResult
    last_instructions: str                     # what the last implement call was told
    pending_check: dict[str, Any] | None       # the VerifierAction run_check_step executes
    unrunnable_streak: int                     # consecutive checks that could not run
    verify_route: str | None                   # where verifier_step / run_check_step sent the run
    acceptance: dict[str, Any] | None          # the AcceptanceVerdict after post-processing
    acceptance_route: str | None               # where acceptance_gate sent the run
    decision: dict[str, Any] | None
    change_summary: str
    attempts: Annotated[list[dict[str, Any]], operator.add]
    reported_bugs: Annotated[list[dict[str, Any]], operator.add]
    triaged_titles: list[str]                 # reports the triage role has labelled
    filed_bugs: list[dict[str, Any]]          # {bead_id, title, where, severity}
    implementer_stopped: bool                 # the last implement call reported blocks_task yes
    blocking_bug: dict[str, Any] | None       # the bug currently routed to remediate
    remediations: Annotated[list[dict[str, Any]], operator.add]  # {bead_id, outcome}, by remediate
    triage_route: str | None                  # where the last triage sent the run
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
        "check_hints": {"type": "array", "items": {"type": "string"}},
        "conventions": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "relevant_files", "conventions", "risks"],
    "additionalProperties": False,
}

BUG_PROTOCOL = """If you discover a defect in existing code outside this task's scope (not the failing tests you were asked to make pass, and not your own change), do not fix it or silently work around it.
If the defect blocks your task, stop and report it.
If it does not block your task, finish your work and append the report.
Use one <bug>...</bug> block per defect, with each field on its own line:
title: short description; where: file:line; evidence: what you ran or saw.
blocks_task: yes|no (your opinion; Alloy decides)."""


def _task_layer(brief: str, acceptance: str) -> str:
    return f"{brief}\n\n## Acceptance criteria\n{acceptance or '(none stated)'}"


def _run_layer(context: dict[str, Any] | None) -> str:
    return f"## Repository context\n{_render_context(context)}"


BEAD_DESIGN_OUTRANKS_MEMORY = "Bead design notes outrank project memory."


def _project_layer(memory: str) -> str:
    """The rendered project memory block plus the one sentence that ranks it
    below the bead's own design notes; empty when there is no memory."""
    return f"{memory}\n\n{BEAD_DESIGN_OUTRANKS_MEMORY}" if memory else ""


def _diff_section(diff: str) -> str:
    clipped = clip_diff(diff).rstrip("\n")
    return f"## Current diff\n```diff\n{clipped}\n```"


CONTEXT_STATIC = f"""You are gathering context for another agent that will implement this task.
Read the repository. Do not modify any file.

Produce a context packet:
- summary: how this repo is laid out and where this change belongs (<= 300 words)
- relevant_files: paths the implementer will most likely touch or read
- check_hints (optional): commands this repo's files, scripts or CI config suggest
  for running tests, lint or build; a verifier decides what actually runs
- conventions: naming, structure and style rules an outsider would get wrong
- risks: things that could make this change break something else

{BUG_PROTOCOL}"""


def context_prompt(brief: str, acceptance: str, *, memory: str = "") -> str:
    return assemble(
        CONTEXT_STATIC, _project_layer(memory), "", _task_layer(brief, acceptance), ""
    ).text


ESTIMATE_STATIC = """You are estimating how hard this task is
You are read-only: do not modify any file. Choose one complexity level:
- simple: one file, obvious change, tests are the spec
- medium: a few files or one new concept
- complex: cross-cutting, concurrency, new subsystem, ambiguous acceptance

Return complexity, reason and confidence in the required structured output."""


def estimate_prompt(
    brief: str, acceptance: str, context: dict[str, Any], *, memory: str = ""
) -> str:
    return assemble(
        ESTIMATE_STATIC,
        _project_layer(memory),
        _run_layer(context),
        _task_layer(brief, acceptance),
        "",
    ).text


TESTS_STATIC = f"""Write failing tests for this task. Do not implement the behavior itself.

Requirements:
- Add tests that encode the acceptance criteria, following this repo's existing test conventions.
- The tests must fail right now, because the behavior does not exist yet.
- Do not write or modify implementation code. Tests only.
- Do not weaken or delete existing tests.
- Alloy owns task tracking, verification and git: do not run `bd`, do not commit,
  and do not run the whole test suite -- run only the tests you wrote.

When you are done, answer with the structured output: `summary` (one paragraph naming which
test files you added or changed and what each asserts) and `baseline_checks`, the exact
shell commands, each with its purpose, that Alloy will run to demonstrate the requested
behaviour is not implemented yet. Every command must target only the tests you wrote
(e.g. one test file or node id), never the whole suite, and must be red right now.
Alloy runs them itself and decides from the exit codes.

{BUG_PROTOCOL}"""

TESTS_RESUMED = (
    "This continues your session: the task brief, acceptance criteria and repository "
    "context are the ones you already have."
)


def tests_prompt(
    brief: str,
    acceptance: str,
    context: dict[str, Any],
    instructions: str = "",
    *,
    memory: str = "",
    resumed: bool = False,
) -> str:
    """`resumed` is the continuation variant sent into the tests writer's own
    session: the brief and repository context are already in its window, so
    only the continuation note, the baseline failure explanation and the rules
    are repeated."""
    volatile: list[str] = []
    if resumed:
        volatile.append(TESTS_RESUMED)
    if instructions:
        volatile.append(f"## Required changes this iteration\n{instructions}")
    return assemble(
        TESTS_STATIC,
        _project_layer(memory),
        "" if resumed else _run_layer(context),
        "" if resumed else _task_layer(brief, acceptance),
        "\n\n".join(volatile),
    ).text


tests_prompt.__test__ = False  # This prompt helper may be imported by pytest modules.


def extract_summary(text: str) -> str:
    """Return the last marked summary, or clipped text when no block is present."""
    summaries = re.findall(r"<summary>(.*?)</summary>", text, re.DOTALL)
    return summaries[-1].strip() if summaries else clip(text, 600)


IMPLEMENT_STATIC = f"""Implement the smallest change that makes the failing tests pass.

Rules:
- Change implementation code, not the tests, unless a test is provably wrong about the stated acceptance criteria -- and say so explicitly if you do.
- A bug in tests written for THIS task is in scope: use the tests provably wrong permission above, not a <bug> block.
- Do not disable, skip or loosen assertions to get green.
- Keep the change minimal and consistent with the repo's conventions.
- Alloy owns task tracking, verification and git: do not run `bd`, do not commit, and do not run the whole test suite -- run the tests relevant to your change; Alloy runs the full suite when you finish.

Finish with one paragraph inside <summary> and </summary> tags describing what you changed and why, including 'tests edited: <paths or none>'.

{BUG_PROTOCOL}"""


def implement_prompt(
    brief: str,
    acceptance: str,
    context: dict[str, Any],
    instructions: str,
    history: list[dict[str, Any]],
    failed_check: dict[str, Any] | None = None,
    baseline_checks: list[dict[str, Any]] | None = None,
    *,
    diff: str = "",
    previous_instructions: str = "",
    memory: str = "",
) -> str:
    task = [_task_layer(brief, acceptance)]
    if baseline_checks:
        task.append("## Checks that must go green\n" + _render_checks(baseline_checks))
    volatile: list[str] = []
    if failed_check:
        # A repair iteration: a required check went red and the run came
        # straight back here without a judge call.
        check = CheckResult.model_validate(failed_check)
        volatile.append(
            "## Failed check\n"
            f"`{check.command}` -> exit {check.exit_code} ({check.headline()})\n\n"
            f"{check.output_tail}"
        )
        volatile.append(_diff_section(diff))
        if previous_instructions:
            volatile.append(f"## Previous repair instructions\n{previous_instructions}")
    if history:
        volatile.append("## Previous attempts\n" + _render_history(history))
    if instructions:
        volatile.append(f"## Required changes this iteration\n{instructions}")
    return assemble(
        IMPLEMENT_STATIC,
        _project_layer(memory),
        _run_layer(context),
        "\n\n".join(task),
        "\n\n".join(volatile),
    ).text


def triage_prompt(
    brief: str,
    acceptance: str,
    checks: list[dict[str, Any]],
    report: dict[str, Any],
    filed: list[dict[str, Any]],
    remediations: list[dict[str, Any]],
    project_context: str | None,
) -> str:
    bug = BugReport.model_validate(report)
    blocks = {True: "yes", False: "no"}.get(bug.blocks_task, "(not stated)")
    filed_text = "\n".join(
        f"- {item.get('bead_id') or '(unfiled)'}: {item.get('title', '')} "
        f"[{item.get('severity', '')}] at {item.get('where') or '?'}"
        for item in filed
    ) or "(none yet)"
    remediation_text = "\n".join(
        f"- {item.get('bead_id') or '?'}: {item.get('outcome', '')}" for item in remediations
    ) or "(none yet)"
    sections = [
        "You are triaging a bug report from a coding agent. You are read-only: you cannot "
        "edit code; you only label the report so Alloy can decide what happens next.",
        brief,
        f"## Acceptance criteria\n{acceptance or '(none stated)'}",
        f"## Current test results\n{_render_results(checks)}",
        "## The report\n"
        f"reporter: {bug.reporter or 'unknown'} role, iteration {bug.iteration}\n"
        f"title: {bug.title}\n"
        f"where: {bug.where or '(not given)'}\n"
        f"evidence: {bug.evidence or '(none given)'}\n"
        f"reporter's opinion, blocks_task: {blocks}",
        f"## Bugs already filed in this run\n{filed_text}",
        f"## Remediations already performed in this run\n{remediation_text}",
    ]
    if project_context:
        sections.append(f"## Project context\n{project_context}")
    sections.append(
        "Choose exactly one severity:\n"
        "- \"not-a-bug\"    -- the report is the task itself, the tests it was asked to make "
        "pass, or a defect that only appears with this task's changes\n"
        "- \"duplicate\"    -- it matches a bug already filed in this run (listed above)\n"
        "- \"non-blocking\" -- a real pre-existing defect the task can finish without; "
        "it is filed for later and stays out of this task's scope\n"
        "- \"blocking\"     -- a real pre-existing defect the task cannot finish without; "
        "Alloy fixes it autonomously in its own bead before the task continues\n"
        "- \"needs-human\"  -- it blocks the task but the fix needs an architectural change "
        "or a decision outside the task (a schema or public API change, a dependency swap, "
        "behaviour the acceptance criteria contradict), or the remediations already done in "
        "this run show remediation is spiralling. This is the operator's carve-out, not the "
        "default.\n\n"
        "Weigh the remediations already performed in this run against the progress made: "
        "there is no fixed cap, but each one spends the task's budget, and a run that keeps "
        "turning up blocking bugs is better handed to a human. Return severity, reason and "
        "confidence in the required structured output."
    )
    return "\n\n".join(sections)


def render_project_context(
    snapshot: ProjectSnapshot,
    brief: str,
    run_history: list[Any],
    remediations: list[dict[str, Any]],
    *,
    iteration: int | None = None,
) -> str:
    """The project context packet: brief, bead graph, progress -- clipped to a
    fixed size so the scope role sees the whole project, never a transcript."""
    history_text = "\n".join(
        item if isinstance(item, str) else Attempt.model_validate(item).render()
        for item in run_history
    ) or "(none)"
    remediation_text = "\n".join(
        f"- {item.get('bead_id') or '?'}: {item.get('outcome', '')}" for item in remediations
    ) or "(none yet)"
    stats_text = ", ".join(f"{key}={value}" for key, value in snapshot.stats.items()) or "(unknown)"
    progress = [f"stats: {stats_text}"]
    if iteration is not None:
        progress.append(f"parent run iteration: {iteration}")
    sections = [
        f"## Project brief ({snapshot.brief_source or 'none'})\n{brief or '(no project brief)'}",
        "## Bead graph\n"
        f"### Epic\n{snapshot.epic or '(no parent epic)'}\n\n"
        f"### Open and in-progress beads\n{chr(10).join(snapshot.open_beads) or '(none)'}\n\n"
        f"### Bugs Alloy has filed\n{chr(10).join(snapshot.filed_bugs) or '(none)'}",
        "## Progress\n"
        f"{chr(10).join(progress)}\n\n"
        f"### Parent run attempt history\n{history_text}\n\n"
        f"### Remediations already performed in this run\n{remediation_text}",
    ]
    text = "\n\n".join(sections)
    # `clip` adds its elision marker on top of the budget; the packet's cap is hard.
    budget = PROJECT_CONTEXT_CHARS
    clipped = clip(text, budget)
    while len(clipped) > PROJECT_CONTEXT_CHARS and budget > 0:
        budget -= len(clipped) - PROJECT_CONTEXT_CHARS
        clipped = clip(text, budget)
    return clipped


def scope_prompt(
    project_context: str,
    parent_brief: str,
    parent_acceptance: str,
    bug_brief: str,
    diffstat: str,
    diff: str,
) -> str:
    return "\n\n".join([
        "You are deciding whether a bug fix is safe to merge into a paused task. You are "
        "read-only: you cannot edit code; you only label the fix so Alloy can decide "
        "whether it lands.",
        f"## Project context\n{project_context}",
        f"## The paused task (parent)\n{parent_brief}",
        f"## Parent acceptance criteria\n{parent_acceptance or '(none stated)'}",
        f"## The bug being fixed\n{bug_brief}",
        f"## Diffstat\n{diffstat}",
        f"## The fix\n```diff\n{clip_diff(diff)}\n```",
        "Choose exactly one verdict:\n"
        "- \"merge\"         -- the change is what this defect requires and nothing more; "
        "it fits what the project brief and bead graph say the project is doing\n"
        "- \"too-broad\"     -- an architecture-sized change for a bug fix: new subsystems, "
        "public API or schema changes, broad refactors, dependency swaps, or work that "
        "belongs to another open bead; judge this against the project brief and the bead "
        "graph, not against a file count\n"
        "- \"subverts-task\" -- it changes behaviour the parent's acceptance criteria rely "
        "on, so the paused task would pass or fail for the wrong reason\n\n"
        "Return verdict, reason and confidence in the required structured output.",
    ])


def _diffstat(diff: str) -> str:
    files = insertions = deletions = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith("+") and not line.startswith("+++"):
            insertions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return f"{files} files changed, {insertions} insertions(+), {deletions} deletions(-)"


async def scope_gate(ctx: RunContext, bug_bead: Any, diff: str) -> ScopeVerdict:
    """Ask the scope role whether `diff` (a remediation child's fix) may merge
    into `ctx`'s task. Never `merge` by default: an unverifiable merge into a
    paused task is blind, so every failure reads as too-broad."""
    default = ScopeVerdict(verdict="too-broad")
    try:
        spec = ctx.recipe.role("scope")
        prompt = scope_prompt(
            ctx.project_context(),
            ctx.bead.task_brief(),
            ctx.bead.acceptance_criteria,
            bug_bead.task_brief(),
            _diffstat(diff),
            diff,
        )
    except Exception as exc:
        default.reason = f"scope role failed: {exc}"
        return default
    verdict = await classify(ctx, "scope", spec, prompt, model_cls=ScopeVerdict, default=default)
    if verdict is default:
        default.reason = f"scope role failed: {default.reason.removeprefix('scope failed: ')}"
    return verdict


async def scope_merge_gate(ctx: RunContext, bug_bead: Any, diff: str) -> tuple[bool, str]:
    """`Engine.MergeGate` adapter: only `merge` proceeds; every other label is
    returned with the label so the parent's note says why the fix stayed out."""
    verdict = await scope_gate(ctx, bug_bead, diff)
    if verdict.verdict == "merge":
        return True, verdict.reason
    return False, f"{verdict.verdict}: {verdict.reason}"


def bug_acceptance(report: BugReport) -> str:
    return (
        f"The defect no longer reproduces: {report.evidence or report.title}. "
        "The existing test suite stays green. The change touches only what this defect "
        "requires -- no refactors, no API or schema changes."
    )


def bug_description(report: BugReport, verdict: BugTriage, parent_id: str, run_id: str) -> str:
    return (
        f"title: {report.title}\n"
        f"where: {report.where or '(not given)'}\n"
        f"evidence: {report.evidence or '(none given)'}\n"
        f"blocks_task (reporter's opinion): {report.blocks_task}\n"
        f"reported by the {report.reporter or 'unknown'} role in iteration {report.iteration}\n"
        f"triage: {verdict.severity} (confidence {verdict.confidence:.2f}) -- {verdict.reason}\n"
        f"parent bead: {parent_id}; run: {run_id}"
    )


JUDGE_STATIC = """You are judging whether a coding task is complete. You cannot edit code;
you only decide what happens next.

Choose exactly one decision:
- "done"      -- tests pass and the diff genuinely satisfies the acceptance criteria
- "retry"     -- a specific, well-understood fix remains; put it in next_instructions
- "consilium" -- attempts are going in circles and independent opinions would help
- "human"     -- the task is ambiguous, or needs a decision or access Alloy does not have
- "abort"     -- the task cannot be completed as specified

Do not answer "done" if tests are failing. Do not answer "done" if the diff is empty.
Judge the diff on merit: green tests that were weakened or skipped are not done.
If the diff edits or deletes tests, say so in `reason` and decide whether the edit
was justified by the acceptance criteria."""


def judge_prompt(
    brief: str,
    acceptance: str,
    context: dict[str, Any],
    diff: str,
    checks: list[dict[str, Any]],
    history: list[dict[str, Any]],
    iteration: int,
    limits_note: str,
    verifier_stop: dict[str, Any] | None = None,
    changed_tests: list[str] | None = None,
    memory: str = "",
) -> str:
    volatile = f"""{_diff_section(diff)}

## Tests changed by the implementer
{chr(10).join(changed_tests or []) or "(none)"}

## Test results
{_render_results(checks, verifier_stop)}

## Attempt history
{_render_history(history) or "(first attempt)"}

## Budget
iteration {iteration}; {limits_note}"""
    return assemble(
        JUDGE_STATIC,
        _project_layer(memory),
        _run_layer(context),
        _task_layer(brief, acceptance),
        volatile,
    ).text


ACCEPTANCE_STATIC = """You are deciding whether there is enough evidence to call a coding task complete.
You are read-only: you cannot edit code and you never run anything yourself. Green
checks are not the same as a complete task: the checks may not cover an acceptance
criterion, or may encode the same misunderstanding as the implementation.

Choose exactly one decision:
- "accept"      -- the evidence covers every acceptance criterion and the diff satisfies them
- "verify_more" -- a specific criterion or risk is still unverified; say which in `reason`
- "repair"      -- the diff visibly falls short of a criterion; say what must change in `reason`
- "escalate"    -- the evidence is ambiguous or the call needs a stronger judge

Give a confidence between 0 and 1. Tests that were weakened, skipped or deleted are
not evidence; say so and do not accept."""


def acceptance_prompt(
    acceptance: str,
    diff: str,
    changed_tests: list[str],
    checks_this_iteration: list[dict[str, Any]],
    verifier_stop: dict[str, Any] | None,
) -> str:
    task = f"## Acceptance criteria\n{acceptance or '(none stated)'}"
    volatile = f"""{_diff_section(diff)}

## Tests changed by the implementer
{chr(10).join(changed_tests) or "(none)"}

## Check evidence (this iteration)
{_render_results(checks_this_iteration, verifier_stop)}"""
    return assemble(ACCEPTANCE_STATIC, "", "", task, volatile).text


VERIFIER_STATIC = """You are choosing the next verification check for a coding task. You are read-only:
you cannot edit code and you never run anything yourself. Do not modify any file. Alloy runs
the one command you name, in the worktree root, exactly as written, and shows you the result.

Answer with the structured output. Either:
- action "run": exactly one shell command Alloy will run in the worktree root, its
  purpose, its kind (regression, targeted, lint, typecheck, build or custom -- any
  project script counts as custom) and whether it is required. A red required check
  sends the task straight back to the implementer; a red optional check is only
  reported to you.
- action "stop": when the evidence is sufficient. Give the reason and list the
  remaining_risks you could not check.

Prefer the most targeted check that would move the evidence: the tests written for
this task first, then what the diff could have broken, then the wider suite, lint or
build. Do not repeat a check whose result cannot have changed. Never claim to have
run anything yourself."""

VERIFIER_RESUMED = (
    "This continues your session: the task brief, acceptance criteria and repository "
    "context are the ones you already have, and the tests are the ones you wrote."
)


def verifier_prompt(
    brief: str,
    acceptance: str,
    context: dict[str, Any],
    diff: str,
    changed_files: list[str],
    checks_this_run: list[dict[str, Any]],
    iteration: int,
    checks_left_iteration: int,
    checks_left_run: int,
    history: list[dict[str, Any]],
    baseline_checks: list[dict[str, Any]] | None = None,
    instructions: str = "",
    *,
    memory: str = "",
    resumed: bool = False,
) -> str:
    """`resumed` is the continuation variant sent into the tests writer's own
    session: it drops the task brief, acceptance criteria and repository context
    (already in the window) and keeps the evidence, the diff and the budget."""
    results = [CheckResult.model_validate(item) for item in checks_this_run]
    if results:
        last = results[-1]
        last_text = f"`{last.command}` -> {last.headline()}\n\n{last.output_tail}"
        if not last.runnable:
            last_text += (
                f"\n\nThe previous command could not run: `{last.command}` -> "
                f"{last.headline()}. Name a command that exists in this repository, "
                "or stop."
            )
    else:
        last_text = "(nothing has run yet this run)"
    hints = f"## Hints from the repository (not yet verified)\n{_render_check_hints(context)}"
    baseline = (
        "## Baseline commands (the tests role's targeted checks; red before implementation)\n"
        f"{_render_checks(baseline_checks or []) or '(none)'}"
    )
    if resumed:
        run = hints
        task = baseline
    else:
        run = f"{_run_layer(context)}\n\n{hints}"
        task = f"{_task_layer(brief, acceptance)}\n\n{baseline}"
    volatile = f"""## Changed files
{chr(10).join(changed_files) or "(no changes)"}

{_diff_section(diff)}

## Checks run so far in this run
{chr(10).join(_render_check_lines(results)) or "(none yet)"}

## Last result
{last_text}

## Acceptance gate
{instructions or "(not consulted yet this iteration)"}

## Attempt history
{_render_history(history) or "(first attempt)"}

## Budget
iteration {iteration}; {checks_left_iteration} more check(s) allowed this iteration, \
{checks_left_run} more in this run"""
    if resumed:
        volatile = f"{VERIFIER_RESUMED}\n\n{volatile}"
    return assemble(VERIFIER_STATIC, _project_layer(memory), run, task, volatile).text


CRITIC_STATIC = """You are one of several independent critics reviewing a stuck coding task.
You are read-only: do not modify any file. You cannot see the other critics' opinions,
and that is deliberate -- give your own honest reading.

Identify the single most likely root cause of the failure and the concrete fix.
Be specific about files and behavior. If you believe the tests are wrong rather than
the implementation, say that explicitly."""


def critic_prompt(evidence: str) -> str:
    return assemble(CRITIC_STATIC, "", "", "", evidence).text


SYNTHESIZE_STATIC = """Several independent critics reviewed a stuck coding task. Reconcile their
opinions into one instruction set for the implementer. You are read-only.

Where the critics agree, treat it as likely true. Where they disagree, decide which
reading the evidence actually supports and say why. Then write direct, concrete
instructions for the implementer: which files to change and what the change must do.
Output the instructions as prose, no preamble."""


def synthesize_prompt(evidence: str, critiques: list[dict[str, Any]]) -> str:
    rendered = "\n\n".join(
        f"### Critic {index + 1} ({item.get('critic', 'unknown')}, "
        f"confidence {item.get('confidence', 0)})\n"
        f"root cause: {item.get('root_cause', '')}\n"
        f"evidence: {item.get('evidence', '')}\n"
        f"suggested fix: {item.get('suggested_fix', '')}"
        for index, item in enumerate(critiques)
    )
    volatile = f"{evidence}\n\n## Independent opinions\n{rendered}"
    return assemble(SYNTHESIZE_STATIC, "", "", "", volatile).text


HARVEST_STATIC = """You are harvesting a durable lesson from a finished coding task run.
You are read-only: do not modify any file.

Review the evidence from this run, any human guidance, and the repository lessons
already recorded. Return a structured answer with scope, key, lesson and confidence:
- scope "repo" -- a lesson worth remembering for future runs on this repository
- scope "task" -- an insight specific to this bead only
- scope "none" -- nothing worth recording

Choose a short snake_case key when scope is repo. Prefer updating an existing lesson
key when the new insight refines the same theme."""


def harvest_prompt(
    evidence: str,
    *,
    human_note: str = "",
    existing_lessons: dict[str, str] | None = None,
) -> str:
    lessons = "\n".join(
        f"- {key}: {body}" for key, body in sorted((existing_lessons or {}).items())
    )
    volatile = (
        f"{evidence}\n\n"
        f"## Human note\n{human_note.strip() or '(none)'}\n\n"
        f"## Existing repository lessons\n{lessons or '(none)'}"
    )
    return assemble(HARVEST_STATIC, "", "", "", volatile).text


def existing_lessons(memory: ProjectMemory | None) -> dict[str, str]:
    """The live alloy:lesson:* entries, key -> provenance-stripped body."""
    if memory is None:
        return {}
    return {
        key: entry.body
        for key, entry in memory.entries.items()
        if key.startswith(LESSON_KEY_PREFIX)
    }


# --------------------------------------------------------------------------
# rendering helpers -- these are what keep state small
# --------------------------------------------------------------------------


def _render_check_hints(context: dict[str, Any] | None) -> str:
    """Commands the context role, the bead or autodetection suggested. None of
    them has run; the verifier decides whether any of them is worth running."""
    hints = (context or {}).get("check_hints") or []
    if not hints:
        return "(none; find the project's own test, lint and build commands)"
    return "\n".join(f"- `{hint}`" for hint in hints)


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


def _render_checks(checks: list[dict[str, Any]]) -> str:
    lines = []
    for raw in checks:
        check = CheckRequest.model_validate(raw)
        lines.append(f"- `{check.command}`" + (f" -- {check.purpose}" if check.purpose else ""))
    return "\n".join(lines)


def _render_check_lines(results: list[CheckResult]) -> list[str]:
    return [
        f"#{index} [{result.kind}] `{result.command}` -> {result.headline()}"
        + ("" if result.required else " (optional)")
        for index, result in enumerate(results, 1)
    ]


def _render_results(
    checks: list[dict[str, Any]], verifier_stop: dict[str, Any] | None = None
) -> str:
    """The checks of one iteration: every headline, then the last output tail."""
    results = [CheckResult.model_validate(item) for item in checks]
    if results:
        lines = _render_check_lines(results)
        lines += ["", f"Last output (`{results[-1].command}`):", results[-1].output_tail]
    else:
        lines = ["(no checks were run this iteration)"]
    if verifier_stop:
        stop = VerifierAction.model_validate(verifier_stop)
        lines.append(f"\nVerifier stopped: {stop.reason or '(no reason given)'}")
        if stop.remaining_risks:
            lines.append("Remaining risks: " + "; ".join(stop.remaining_risks))
    return "\n".join(lines)


def _checks_of(state: TddState, iteration: int) -> list[dict[str, Any]]:
    return [item for item in state.get("checks") or [] if item.get("iteration") == iteration]


def _checks_headline(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return "(no checks run)"
    results = [CheckResult.model_validate(item) for item in checks]
    return "; ".join(f"[{result.kind}] {result.headline()}" for result in results)


def _render_history(history: list[dict[str, Any]]) -> str:
    return "\n".join(Attempt.model_validate(item).render() for item in history[-5:])


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
    return str(
        assemble(
            "",
            _project_layer(state.get("memory_block", "")),
            _run_layer(state.get("context")),
            _task_layer(ctx.bead.task_brief(), ctx.bead.acceptance_criteria),
            volatile,
        ).text
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
            role, replace(spec, fallback=None), resumed_prompt,
            schema=schema, iteration=iteration, resume_session=session_id,
        )
        if result.ok:
            return result
        log.warning(
            "%s: resuming session %s on %s failed (%s); running fresh",
            role, session_id, spec.label, (result.error or f"exit {result.exit_code}")[:200],
        )
    return await ctx.call(role, spec, prompt, schema=schema, iteration=iteration)


class TriageFailure:
    """`classify` default for the triage role: never a severity, so the run
    can only go to the human gate when triage did not answer."""

    severity = None

    def __init__(self) -> None:
        self.reason = ""


def untriaged(state: TddState) -> list[dict[str, Any]]:
    done = set(state.get("triaged_titles") or [])
    return [bug for bug in state.get("reported_bugs", []) if bug["title"] not in done]


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
            context_prompt(
                ctx.bead.task_brief(),
                ctx.bead.acceptance_criteria,
                memory=state.get("memory_block", ""),
            ),
            schema=CONTEXT_SCHEMA,
            iteration=0,
        )
        reported_bugs = _capture_bugs("context", result, state, 0)
        packet = _context_from(result)
        packet.check_hints = _check_hints(
            packet.check_hints, ctx.bead.check_hint, (ctx.worktree.path, ctx.worktrees.repo),
            memory_hints=parse_check_hints(state.get("memory_check_hints", "")),
        )
        return {
            "reported_bugs": reported_bugs,
            "context": packet.compact(),
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
                    ctx.bead.task_brief(),
                    ctx.bead.acceptance_criteria,
                    state.get("context", {}),
                    memory=state.get("memory_block", ""),
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
        # A repair pass (green or unrunnable baseline) continues the session
        # that wrote the tests; the first pass has no session yet.
        session_id = resumable_session(spec, state.get("tests_session"))
        prompt_args = (
            ctx.bead.task_brief(),
            ctx.bead.acceptance_criteria,
            state.get("context", {}),
            state.get("instructions", ""),
        )
        prompt_kwargs = dict(memory=state.get("memory_block", ""))
        result = await call_in_session(
            ctx, "tests", spec,
            tests_prompt(*prompt_args, **prompt_kwargs),
            tests_prompt(*prompt_args, **prompt_kwargs, resumed=True),
            session_id=session_id,
            schema=TestsOutput.schema_for_agents(),
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
                    retry_at=_retry_at_iso(result),
                ).model_dump(),
            }
        output = _tests_output_from(result)
        update: dict[str, Any] = {
            "stage": "tests",
            "instructions": "",
            "reported_bugs": reported_bugs,
            "baseline_checks": [c.model_dump(mode="json") for c in output.baseline_checks],
        }
        if result.session_id:
            update["tests_session"] = {"runner": result.runner, "session_id": result.session_id}
        return update

    def route_after_tests(state: TddState) -> str:
        """A harness that cannot write the tests has nothing for the implementer
        to aim at: pause for a human, who decides whether to retry or cancel."""
        decision = (state.get("decision") or {}).get("decision")
        if decision == "abort":
            return "finish"
        if decision == "human":
            return "human_gate"
        return "prove_red"

    def route_after_human(state: TddState) -> str:
        return state.get("resume_target") or "implement"

    async def prove_red(state: TddState) -> dict[str, Any]:
        """Run the tests role's baseline checks. RED -- every check runnable and
        failing -- is the only way forward; anything else sends the tests role
        back with the facts, a bounded number of times, then a human."""
        ctx.set_stage("baseline")
        checks = [CheckRequest.model_validate(c) for c in state.get("baseline_checks", [])]
        results = [await ctx.run_check(check) for check in checks]
        update: dict[str, Any] = {
            "baseline": [r.model_dump(mode="json") for r in results],
            "stage": "baseline",
            "instructions": "",
        }
        problems: list[str] = []
        unrunnable = False
        if not checks:
            problems.append("no baseline command given")
            unrunnable = True
        for result in results:
            if not result.runnable:
                problems.append(f"`{result.command}`: {result.headline()}")
                unrunnable = True
            elif result.ok:
                problems.append(f"`{result.command}`: passed")
        if not problems:
            # The implementer starts from here: remember what every test file
            # looked like so the gates can tell its edits from the tests role's.
            test_paths = [
                path for path in ctx.worktrees.changed_files(ctx.worktree) if is_test_path(path)
            ]
            update["test_fingerprints"] = ctx.worktrees.fingerprints(ctx.worktree, test_paths)
            return update
        repairs = state.get("baseline_repairs", 0) + 1
        headline = "baseline not runnable" if unrunnable else "baseline unexpectedly green"
        detail = f"{headline}: " + "; ".join(problems)
        if repairs > ctx.recipe.verification.max_baseline_repairs:
            update.update(
                resume_to="tests",
                decision=JudgeDecision(
                    decision="human",
                    reason=detail,
                    next_instructions="The tests role could not produce a red, runnable "
                    "baseline. Tell it what to fix; the tests stage runs again.",
                ).model_dump(),
            )
            return update
        update.update(
            baseline_repairs=repairs,
            instructions=(
                f"The baseline you supplied did not prove the behaviour is missing "
                f"({detail}). Every baseline command must run and fail right now. Fix "
                "the tests or the commands and answer with the corrected baseline_checks."
            ),
        )
        return update

    def route_after_prove_red(state: TddState) -> str:
        if (state.get("decision") or {}).get("decision") == "human" and state.get("resume_to"):
            return "human_gate"
        if state.get("instructions"):
            return "tests"
        return "implement"

    async def implement(state: TddState) -> dict[str, Any]:
        iteration = state.get("iteration", 0) + 1
        ctx.set_stage("implement", iteration=iteration)
        spec = ctx.role_spec("implement", state)
        if ctx.recipe.complexity.routing == "live" and ctx.recipe.role("implement").tiered:
            ctx.set_dispatch_tier(state.get("complexity"))
        last_check = state.get("last_check")
        repairing = bool(last_check) and _is_red(last_check)
        result = await ctx.call(
            "implement",
            spec,
            implement_prompt(
                ctx.bead.task_brief(),
                ctx.bead.acceptance_criteria,
                state.get("context", {}),
                state.get("instructions", ""),
                state.get("attempts", []),
                last_check if repairing else None,
                baseline_checks=state.get("baseline_checks", []),
                diff=ctx.diff() if repairing else "",
                previous_instructions=state.get("last_instructions", "") if repairing else "",
                memory=state.get("memory_block", ""),
            ),
            iteration=iteration,
        )
        reported_bugs = _capture_bugs("implement", result, state, iteration)
        return {
            "reported_bugs": reported_bugs,
            "implementer_stopped": any(bug.get("blocks_task") for bug in reported_bugs),
            "iteration": iteration,
            "iteration_checks": 0,
            "unrunnable_streak": 0,
            "last_instructions": state.get("instructions", ""),
            "stage": "implement",
            "instructions": "",
            "implementer": result.runner,
            "change_summary": extract_summary(result.text) if result.ok else result.summary,
        }

    def route_after_implement(state: TddState) -> str:
        return "triage" if untriaged(state) else "verifier_step"

    def _first_available(spec: RoleSpec) -> RoleSpec | None:
        """The first entry of the fallback chain whose runner is installed.

        Checked up front so a missing primary (Jev without a key) does not
        leave a failed ledger row per report; `ctx.call` still falls back
        on timeouts and non-zero exits."""
        chain: RoleSpec | None = spec
        while chain is not None:
            if ctx.registry.available(chain.runner):
                return chain
            chain = chain.fallback
        return None

    def _park(reason: str, question: str) -> dict[str, Any]:
        return {
            "triage_route": "human_gate",
            "resume_to": "implement",
            "decision": JudgeDecision(
                decision="human", reason=reason, next_instructions=question
            ).model_dump(),
        }

    def _file_bug(report: BugReport, verdict: BugTriage) -> str:
        if ctx.beads is None:
            return "(unfiled)"
        severity = verdict.severity
        labels = [LABEL_BUG] + ([LABEL_HUMAN] if severity == "needs-human" else [])
        metadata = {
            key: ctx.bead.metadata[key] for key in (META_RECIPE,) if ctx.bead.metadata.get(key)
        }
        metadata[META_DISCOVERED_IN_RUN] = ctx.run_id
        bug_id = ctx.beads.create_bug(
            title=report.title,
            description=bug_description(report, verdict, ctx.bead.id, ctx.run_id),
            acceptance=bug_acceptance(report),
            discovered_from=ctx.bead.id,
            priority=3 if severity == "non-blocking" else 1,
            labels=labels,
            metadata=metadata,
            claim=severity == "blocking" and not ctx.is_child,
        )
        if severity == "needs-human":
            ctx.beads.add_dependency(ctx.bead.id, bug_id)
        return bug_id

    async def triage(state: TddState) -> dict[str, Any]:
        """Label every untriaged `<bug>` report; file what deserves a bead."""
        iteration = state.get("iteration", 0)
        ctx.set_stage("triage", iteration=iteration)
        configured = ctx.recipe.role("triage")
        triaged = list(state.get("triaged_titles") or [])
        filed = list(state.get("filed_bugs") or [])
        remediations = state.get("remediations") or []
        notes = [state["instructions"]] if state.get("instructions") else []
        project_context = getattr(ctx, "project_context", None)
        update: dict[str, Any] = {
            "stage": "triage", "triaged_titles": triaged, "filed_bugs": filed,
        }
        for raw in untriaged(state):
            report = BugReport.model_validate(raw)
            where = f"{report.where or '?'}: {report.evidence}"
            spec = _first_available(configured)
            if spec is None:
                return {**update, **_park(
                    f"no triage runner is available ({configured.label}) for bug report "
                    f"'{report.title}' at {where}; nothing was filed.",
                    "Install or configure the triage runner, then resume; the report "
                    "is triaged before the implementer runs again.",
                )}
            failure = TriageFailure()
            verdict = await classify(
                ctx, "triage", spec,
                triage_prompt(
                    ctx.bead.task_brief(), ctx.bead.acceptance_criteria,
                    _checks_of(state, iteration), raw, filed, remediations,
                    project_context(state) if callable(project_context) else None,
                ),
                model_cls=BugTriage, default=failure, iteration=iteration,
            )
            if verdict is failure:
                return {**update, **_park(
                    f"{failure.reason} while triaging bug report '{report.title}' at {where}; "
                    "nothing was filed.",
                    "Decide what to do with the report, then resume.",
                )}
            triaged.append(report.title)
            severity = verdict.severity
            if severity == "not-a-bug":
                notes.append(
                    f"Triage rejected your report '{report.title}': it is part of this task. "
                    "Continue and make the tests pass."
                )
                continue
            if severity == "duplicate":
                notes.append(
                    f"Bug '{report.title}' duplicates a report already filed in this run; "
                    "proceed with the task."
                )
                continue
            try:
                bug_id = _file_bug(report, verdict)
            except Exception as exc:
                return {**update, **_park(
                    f"could not file bug '{report.title}' ({severity}) at {where}: {exc}",
                    "File or dismiss the bug by hand, then resume.",
                )}
            entry = {
                "bead_id": bug_id, "title": report.title, "where": report.where,
                "severity": severity,
            }
            filed.append(entry)
            if severity == "non-blocking":
                notes.append(
                    f"Bug '{report.title}' is filed as {bug_id} and is out of scope: "
                    "do not fix it here; proceed with the task."
                )
                continue
            if severity == "blocking":
                if ctx.is_child:
                    # Depth one: a child does not spawn its own child; the
                    # parent parks through this run's outcome.
                    return {**update, "blocking_bug": {**entry, "reason": verdict.reason}, **_park(
                        f"blocking bug '{report.title}' ({bug_id}) found inside remediation "
                        f"child {ctx.run_id}: {verdict.reason}. Remediation is one level "
                        "deep, so the bug is filed unclaimed and this run stops.",
                        f"Fix {bug_id} (or merge its fix into this branch), then resume; "
                        "the implementer continues from there.",
                    )}
                return {
                    **update,
                    "triage_route": "remediate",
                    "blocking_bug": {**entry, "reason": verdict.reason},
                    "instructions": "\n".join(notes),
                }
            return {**update, **_park(
                f"bug '{report.title}' ({bug_id}) needs a human: {verdict.reason}. "
                f"Bead {ctx.bead.id} is now blocked by {bug_id}.",
                f"Resolve {bug_id} (see `bd human list`), then resume; the implementer "
                "runs again with your instructions.",
            )}
        return {
            **update,
            "triage_route": "implement" if state.get("implementer_stopped")
            else "verifier_step",
            "instructions": "\n".join(notes),
        }

    def route_after_triage(state: TddState) -> str:
        return state.get("triage_route") or "verifier_step"

    async def remediate(state: TddState) -> dict[str, Any]:
        """Run the blocking bug as a child of this run and merge its fix in here.

        The parent waits at `remediate:<bug-id>` while the child runs. A merged
        fix sends the parent back to `implement` with an instruction not to
        undo it; anything else (child parked, failed, aborted, guard or scope
        gate refused, merge conflict) parks the parent at the human gate."""
        bug = state.get("blocking_bug") or {}
        bug_id = str(bug.get("bead_id") or "")
        ctx.set_stage(f"remediate:{bug_id}", iteration=state.get("iteration", 0))
        started_at = utcnow().isoformat()
        try:
            result = await ctx.remediate(bug_id)
            outcome = str(getattr(result, "outcome", "") or Outcome.FAILED.value)
            reason = str(getattr(result, "reason", "") or "")
            run_id = getattr(result, "run_id", None)
        except Exception as exc:
            outcome, reason, run_id = Outcome.FAILED.value, str(exc), None
        entry = {
            "bead_id": bug_id, "run_id": run_id, "outcome": outcome, "reason": reason,
            "started_at": started_at, "ended_at": utcnow().isoformat(),
        }
        update: dict[str, Any] = {
            "stage": "remediate", "remediations": [entry], "blocking_bug": None,
        }
        if outcome == Outcome.DONE.value:
            notes = [state["instructions"]] if state.get("instructions") else []
            notes.append(
                f"Bug '{bug.get('title')}' was fixed and merged into this worktree by "
                f"{bug_id}; do not undo it; continue with the task."
            )
            return {**update, "instructions": "\n".join(notes)}
        return {
            **update,
            "resume_to": "implement",
            "decision": JudgeDecision(
                decision="human",
                reason=f"remediation of blocking bug {bug_id} '{bug.get('title')}' "
                f"ended {outcome}: {reason or 'no reason given'}",
                next_instructions=f"Fix {bug_id} (or merge its fix into this branch), "
                "then resume; the implementer continues from there.",
            ).model_dump(),
        }

    def route_after_remediate(state: TddState) -> str:
        remediations = state.get("remediations") or []
        last = remediations[-1] if remediations else {}
        return "implement" if last.get("outcome") == Outcome.DONE.value else "human_gate"

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
            "stage": "verify", "pending_check": None, "verify_route": "acceptance_gate",
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
                reason=f"Alloy stopped verification after {iteration_checks} checks "
                "this iteration",
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
            state.get("context", {}),
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
        )
        # The verifier continues the tests writer's session: the agent that
        # wrote the tests chooses how to verify them, context intact.
        session_id = resumable_session(spec, tests_session)
        try:
            result = await call_in_session(
                ctx, "verifier", spec,
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
                    next_instructions="Revert or keep the verifier's edits, then "
                    "resume; the implementer runs again.",
                ).model_dump(),
            )
            return update
        action = failure if result is None else answer_of(
            "verifier", result, model_cls=VerifierAction, default=failure
        )
        if action is failure or (action.action == "run" and not action.command.strip()):
            why = failure.reason if action is failure else \
                "verifier proposed a run without a command"
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

    async def run_check_step(state: TddState) -> dict[str, Any]:
        """Run the check the verifier proposed, as-is, and record the evidence.

        One command per node: the result is checkpointed before the verifier
        is asked again, so a crash here re-runs only this check. A red
        required check goes straight back to the implementer (through guard,
        so the iteration limits still apply) without a judge call; two
        unrunnable commands in a row ask a human."""
        iteration = state.get("iteration", 0)
        action = VerifierAction.model_validate(state.get("pending_check") or {})
        result = await ctx.run_check(action.to_request())
        result.iteration = iteration
        record = result.model_dump(mode="json")
        checks = [*(state.get("checks") or []), record]
        iteration_checks = int(state.get("iteration_checks", 0) or 0) + 1
        ctx.set_tests_summary(f"{len(checks)} checks, last: {result.headline()}")
        update: dict[str, Any] = {
            "stage": "verify", "checks": [record], "last_check": record,
            "iteration_checks": iteration_checks, "verify_route": "verifier_step",
            "unrunnable_streak": 0,
        }

        if not result.runnable:
            streak = int(state.get("unrunnable_streak", 0) or 0) + 1
            update["unrunnable_streak"] = streak
            if streak >= 2:
                unrunnable = [CheckResult.model_validate(c) for c in checks[-2:]]
                update.update(
                    verify_route="human_gate",
                    resume_to="implement",
                    decision=JudgeDecision(
                        decision="human",
                        reason="the verifier proposed two commands in a row that could "
                        "not run: " + ", ".join(
                            f"`{c.command}` ({c.headline()})" for c in unrunnable
                        ),
                        next_instructions="Check the worktree's toolchain or tell the "
                        "implementer what to verify, then resume; the implementer "
                        "runs again.",
                    ).model_dump(),
                )
            return update

        if result.required and not result.ok:
            reason = f"required check `{result.command}` failed ({result.headline()})"
            update.update(
                verify_route="guard",
                decision=JudgeDecision(
                    decision="retry",
                    reason=reason,
                    next_instructions=(
                        f"The required check `{result.command}` exited "
                        f"{result.exit_code}; see '## Failed check'. Make it pass "
                        "without weakening or skipping it."
                    ),
                ).model_dump(),
                attempts=[Attempt(
                    iteration=iteration,
                    implementer=state.get("implementer")
                    or ctx.role_spec("implement", state).runner,
                    change_summary=state.get("change_summary", ""),
                    checks=_checks_headline(_checks_of({"checks": checks}, iteration)),
                    decision="repair",
                    reason=reason,
                ).model_dump()],
            )
        return update

    def route_after_check(state: TddState) -> str:
        return state.get("verify_route") or "verifier_step"

    def implementer_changed_tests(state: TddState) -> list[str]:
        """Test files whose contents moved since prove_red: edited, deleted or
        added by the implementer, as opposed to written by the tests role."""
        before = state.get("test_fingerprints") or {}
        current = [
            path for path in ctx.worktrees.changed_files(ctx.worktree) if is_test_path(path)
        ]
        paths = sorted(set(before) | set(current))
        after = ctx.worktrees.fingerprints(ctx.worktree, paths)
        return [path for path in paths if before.get(path) != after.get(path)]

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
        changed_tests = implementer_changed_tests(state)
        verdict = await classify(
            ctx, "acceptance", spec,
            acceptance_prompt(
                ctx.bead.acceptance_criteria, ctx.diff(), changed_tests, checks, stop_raw,
            ),
            model_cls=AcceptanceVerdict,
            default=AcceptanceVerdict(decision="escalate", reason="acceptance role failed"),
            iteration=iteration,
        )
        if verdict.decision == "accept" and \
                verdict.confidence < verification.min_acceptance_confidence:
            verdict = AcceptanceVerdict(
                decision="escalate",
                reason=f"{verdict.reason}; confidence below threshold",
                confidence=verdict.confidence,
            )
        if verdict.decision == "verify_more" and \
                iteration_checks >= verification.max_checks_per_iteration:
            verdict = AcceptanceVerdict(
                decision="escalate",
                reason=f"{verdict.reason}; verifier check budget exhausted",
                confidence=verdict.confidence,
            )

        update: dict[str, Any] = {"stage": "acceptance", "acceptance": verdict.model_dump()}
        risks = "; ".join(stop.remaining_risks) if stop and stop.remaining_risks else ""
        row = dict(
            iteration=iteration,
            implementer=state.get("implementer") or ctx.role_spec("implement", state).runner,
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
                    decision="retry", reason=reason, next_instructions=instructions,
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
                instructions=instructions,
            )
        else:
            update["acceptance_route"] = "judge"
        return update

    def route_after_acceptance(state: TddState) -> str:
        return state.get("acceptance_route") or "judge"

    async def judge(state: TddState) -> dict[str, Any]:
        ctx.set_stage("judge", iteration=state.get("iteration", 0))
        spec = ctx.recipe.role("judge")
        diff = ctx.diff()
        changed_tests = implementer_changed_tests(state)
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
        decision = _decision_from(result, state)
        attempt = Attempt(
            iteration=state.get("iteration", 0),
            implementer=state.get("implementer") or ctx.role_spec("implement", state).runner,
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
        # Green means: every required check of this iteration passed.
        tests_green = not any(
            _is_red(check) for check in _checks_of(state, state.get("iteration", 0))
        )

        decision = proposed
        if proposed.decision == "done" and not tests_green:
            decision = JudgeDecision(
                decision="retry",
                reason="judge said done while tests are failing; overridden by Alloy",
                next_instructions=proposed.next_instructions or "Make the failing tests pass.",
                confidence=proposed.confidence,
            )
        elif proposed.decision == "done" and not ctx.worktrees.has_changes(ctx.worktree):
            decision = JudgeDecision(
                decision="retry",
                reason="done proposed while the diff is empty; overridden by Alloy",
                next_instructions=proposed.next_instructions
                or "The worktree has no changes; implement the task.",
                confidence=proposed.confidence,
            )

        iteration = state.get("iteration", 0)
        recorded: dict[str, Any] = {}
        if not any(row.get("iteration") == iteration for row in state.get("attempts") or []):
            # The iteration ends here without a judge or repair row (e.g. the
            # acceptance gate accepted): record it once, with guard's verdict.
            recorded["attempts"] = [Attempt(
                iteration=iteration,
                implementer=state.get("implementer") or ctx.role_spec("implement", state).runner,
                change_summary=state.get("change_summary", ""),
                checks=_checks_headline(_checks_of(state, iteration)),
                decision=decision.decision,
                reason=decision.reason,
                changed_tests=implementer_changed_tests(state),
            ).model_dump()]

        if decision.decision in ("done", "abort", "human"):
            return {"stage": "guard", "decision": decision.model_dump(), **recorded}

        if breach:
            return {
                **recorded,
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
            decision = JudgeDecision(
                decision="retry",
                reason="consilium budget exhausted; downgraded to retry",
                next_instructions=decision.next_instructions
                or "Consilium budget is spent; fix the most likely cause directly.",
            )

        if decision.decision == "consilium":
            return {
                "stage": "guard", "decision": decision.model_dump(), "retries_on_tier": 0,
                **recorded,
            }

        retries = state.get("retries_on_tier", 0) + 1
        update: dict[str, Any] = {
            **recorded,
            "stage": "guard",
            "instructions": decision.next_instructions,
            "decision": decision.model_dump(),
            "retries_on_tier": retries,
        }
        if ctx.recipe.complexity.routing == "live" and \
                retries >= ctx.recipe.complexity.escalate_after_retries:
            previous = state["complexity"]
            level = next_level(previous)
            if level != previous:
                iteration = state.get("iteration", 0)
                reason = f"after {retries} retries (iteration {iteration})"
                ctx.set_complexity(level, "escalation", reason)
                update.update(
                    complexity=level,
                    complexity_source="escalation",
                    retries_on_tier=0,
                    escalations=[{
                        "from": previous, "to": level, "iteration": iteration, "reason": reason,
                    }],
                )
        return update

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
                "last_check": _last_check_summary(state),
                "worktree": str(ctx.worktree.path),
                # journal 38: set when the harness said when it comes back;
                # the scheduler resumes the run itself once it has passed.
                "retry_at": decision.get("retry_at"),
            }
        )
        instructions = payload if isinstance(payload, str) else (payload or {}).get(
            "instructions", ""
        )
        return {
            "instructions": instructions or "Continue; the human provided no extra guidance.",
            "human_note": instructions or "",
            "retries_on_tier": 0,
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
        if outcome is Outcome.DONE:
            remember_check_hints(state)
        return {
            "outcome": outcome.value,
            "outcome_reason": decision.reason,
            "stage": "finished",
        }

    def remember_check_hints(state: TddState) -> None:
        """Persist the commands the verifier could run in this DONE run as
        alloy:check-hints for the next run on this repository. Nothing is
        written when memory is off, no bd is bound, no check was runnable, or
        the stored value already says the same thing."""
        if ctx.beads is None or not ctx.recipe.memory.enabled:
            return
        checks = [CheckResult.model_validate(item) for item in state.get("checks") or []]
        body = format_check_hints(checks)
        if not body or body == state.get("memory_check_hints", ""):
            return
        try:
            ctx.beads.remember(
                CHECK_HINTS_KEY,
                with_provenance(body, ctx.run_id, ctx.bead.id, utcnow().date()),
            )
        except Exception:
            log.warning("could not remember %s", CHECK_HINTS_KEY, exc_info=True)

    async def harvest(state: TddState) -> dict[str, Any]:
        """Ask the harvest role for a durable lesson from this run and persist
        it as alloy:lesson:<key> when it clears the bar. Runs after `finish`
        and never touches `outcome`: a failed or malformed answer, or one
        below the bar, writes nothing."""
        default = HarvestAnswer(scope="none")
        try:
            spec = ctx.recipe.role("harvest")
            prompt = harvest_prompt(
                _evidence_packet(state, ctx, ctx.diff()),
                human_note=state.get("human_note", ""),
                existing_lessons=existing_lessons(ctx.project_memory()),
            )
        except Exception:
            log.warning("harvest skipped", exc_info=True)
            return {}
        answer = await classify(
            ctx, "harvest", spec, prompt,
            model_cls=HarvestAnswer, default=default, iteration=state.get("iteration", 0),
        )
        if answer is default:
            log.info("harvest wrote nothing: %s", default.reason)
            return {}
        remember_lesson(state, answer)
        return {}

    def remember_lesson(state: TddState, answer: HarvestAnswer) -> None:
        """Persist a repo-scoped lesson when memory is on, bd is bound, the
        confidence clears memory.harvest_min_confidence, and the run finished
        DONE or FAILED after at least two implement iterations."""
        if ctx.beads is None or not ctx.recipe.memory.enabled:
            return
        if answer.scope != "repo":
            return
        if answer.confidence < ctx.recipe.memory.harvest_min_confidence:
            return
        if state.get("outcome") not in (Outcome.DONE.value, Outcome.FAILED.value):
            return
        if state.get("iteration", 0) < 2:
            return
        key = answer.memory_key()
        body = answer.lesson.strip()
        if not key or not body:
            return
        try:
            ctx.beads.remember(
                key, with_provenance(body, ctx.run_id, ctx.bead.id, utcnow().date())
            )
            ctx.beads.note(
                ctx.bead.id,
                f"alloy: run {ctx.run_id} harvested repository lesson {key} "
                f"(confidence {answer.confidence:.2f})",
            )
        except Exception:
            log.warning("could not remember %s", key, exc_info=True)

    graph = StateGraph(TddState)
    graph.add_node("context", gather_context)
    graph.add_node("estimate", estimate)
    graph.add_node("tests", write_tests)
    graph.add_node("prove_red", prove_red)
    graph.add_node("implement", implement)
    graph.add_node("triage", triage)
    graph.add_node("remediate", remediate)
    graph.add_node("verifier_step", verifier_step)
    graph.add_node("run_check_step", run_check_step)
    graph.add_node("acceptance_gate", acceptance_gate)
    graph.add_node("judge", judge)
    graph.add_node("guard", guard)
    graph.add_node("critic", critic, input_schema=CriticInput)
    graph.add_node("synthesize", synthesize)
    graph.add_node("human_gate", human_gate)
    graph.add_node("finish", finish)
    graph.add_node("harvest", harvest)

    graph.add_edge(START, "context")
    graph.add_edge("context", "estimate")
    graph.add_edge("estimate", "tests")
    graph.add_conditional_edges("tests", route_after_tests, ["prove_red", "finish", "human_gate"])
    graph.add_conditional_edges(
        "prove_red", route_after_prove_red, ["tests", "implement", "human_gate"]
    )
    graph.add_conditional_edges(
        "implement", route_after_implement, ["triage", "verifier_step"]
    )
    graph.add_conditional_edges(
        "triage", route_after_triage,
        ["remediate", "human_gate", "implement", "verifier_step"],
    )
    graph.add_conditional_edges("remediate", route_after_remediate, ["implement", "human_gate"])
    graph.add_conditional_edges(
        "verifier_step", route_after_verifier,
        ["run_check_step", "acceptance_gate", "guard", "human_gate"],
    )
    graph.add_conditional_edges(
        "run_check_step", route_after_check,
        ["verifier_step", "guard", "human_gate"],
    )
    graph.add_conditional_edges(
        "acceptance_gate", route_after_acceptance,
        ["guard", "verifier_step", "judge"],
    )
    graph.add_edge("judge", "guard")
    graph.add_conditional_edges(
        "guard", route, ["implement", "critic", "human_gate", "finish"]
    )
    graph.add_edge("critic", "synthesize")
    graph.add_edge("synthesize", "implement")
    graph.add_conditional_edges("human_gate", route_after_human, ["implement", "tests"])
    graph.add_edge("finish", "harvest")
    graph.add_edge("harvest", END)

    return graph.compile(checkpointer=ctx.checkpointer)


def initial_state(ctx: RunContext) -> TddState:
    memory = ctx.project_memory()
    return TddState(
        bead_id=ctx.bead.id,
        run_id=ctx.run_id,
        title=ctx.bead.title,
        memory_block=memory.render() if memory is not None else "",
        memory_check_hints=memory.body_of(CHECK_HINTS_KEY) if memory is not None else "",
        iteration=0,
        consiliums=0,
        retries_on_tier=0,
        escalations=[],
        instructions="",
        baseline_checks=[],
        baseline=None,
        baseline_repairs=0,
        tests_session=None,
        test_fingerprints={},
        implementer="",
        checks=[],
        iteration_checks=0,
        pending_check=None,
        unrunnable_streak=0,
        verifier_stop=None,
        last_check=None,
        last_instructions="",
        verify_route=None,
        acceptance=None,
        acceptance_route=None,
        attempts=[],
        reported_bugs=[],
        triaged_titles=[],
        filed_bugs=[],
        implementer_stopped=False,
        blocking_bug=None,
        remediations=[],
        triage_route=None,
        critiques=[],
        change_summary="",
        budget_extensions=0,
        stage="starting",
        outcome=None,
        outcome_reason="",
        limit_hit=None,
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


def _context_from(result) -> ContextPacket:
    if result.structured:
        try:
            return ContextPacket.model_validate(result.structured)
        except Exception:
            pass
    return ContextPacket(summary=clip(result.text, 3000) if result.ok else
                         f"(context gathering failed: {result.error})")


def _tests_output_from(result) -> TestsOutput:
    if result.structured:
        try:
            return TestsOutput.model_validate(result.structured)
        except Exception:
            pass
    return TestsOutput(summary=clip(result.text, 3000))


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
