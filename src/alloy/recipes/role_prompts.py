"""Role prompt construction and shared presentation for the TDD recipe."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from alloy.diffs import clip_diff_per_file
from alloy.models import (
    CONTRADICTION_KEY_PREFIX,
    MEMORY_RENDER_HEADER,
    Attempt,
    BugReport,
    CheckRequest,
    CheckResult,
    ContextPacket,
    ProjectMemory,
    ProjectSnapshot,
    ReviewPlan,
    VerifierAction,
    clip,
)
from alloy.prompts import assemble
from alloy.verify import (
    diff_derived_test_paths,
)

MAX_DIFF_CHARS = 12000
PER_FILE_DIFF_CHARS = 4000
"""Per-file budget inside MAX_DIFF_CHARS so one generated file cannot hide the rest."""
PROJECT_CONTEXT_CHARS = 6000
"""Hard cap on the project context packet handed to the scope and triage roles."""
REMEDIATION_MIN_AGENT_CALLS = 6
"""Recipe minimum for one remediation child (context, estimate, tests,
implement, verifier, judge); the pre-dispatch headroom estimate never goes
below this, so an empty alloy:calibration still gates deterministically."""
log = logging.getLogger(__name__)


def clip_diff(diff: str) -> str:
    """The diff as every prompt sees it: per-file clipped, then capped overall."""
    return clip_diff_per_file(diff, per_file=PER_FILE_DIFF_CHARS, total=MAX_DIFF_CHARS)


CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "relevant_files": {"type": "array", "items": {"type": "string"}},
        "check_hints": {"type": "array", "items": {"type": "string"}},
        "conventions": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "memory_contradictions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "relevant_files", "conventions", "risks"],
    "additionalProperties": False,
}

BUG_PROTOCOL = """If you discover a defect in existing code outside this task's scope (not the failing tests \
you were asked to make pass, and not your own change), do not fix it or silently work around it.
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
- memory_contradictions (optional): project memory entries the repository
  contradicts, each 'key: why'; the memory is left as is and flagged for review

{BUG_PROTOCOL}"""


def context_prompt(brief: str, acceptance: str, *, memory: str = "") -> str:
    return assemble(CONTEXT_STATIC, _project_layer(memory), "", _task_layer(brief, acceptance), "").text


ESTIMATE_STATIC = """You are estimating how hard this task is
You are read-only: do not modify any file. Choose one complexity level:
- simple: one file, obvious change, tests are the spec
- medium: a few files or one new concept
- complex: cross-cutting, concurrency, new subsystem, ambiguous acceptance

Return complexity, reason and confidence in the required structured output."""


def estimate_prompt(
    brief: str,
    acceptance: str,
    context: dict[str, Any],
    *,
    memory: str = "",
    calibration: str = "",
) -> str:
    """`calibration` is the rendered alloy:calibration line; only the estimate
    role sees it, so it joins the project layer here rather than the shared
    memory block."""
    return assemble(
        ESTIMATE_STATIC,
        _project_layer("\n\n".join(part for part in (memory, calibration) if part)),
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


TESTS_REVIEW_STATIC = """You are an independent reviewer of the tests another agent wrote for a task.
You are read-only: do not modify any file, run only read-only commands.

You did not write these tests and you have not seen the author's reasoning. First read the
task and the acceptance criteria and decide, in your own words, what the behaviour must be.
Only then read the tests and compare. Look for:
- a criterion no test would fail without (missing coverage), or a test that would pass for
  a wrong implementation (too weak, asserts nothing that matters);
- a test that encodes a different reading of the task than the acceptance criteria state,
  or invents requirements the task does not ask for;
- a test that pins incidental implementation details and would reject a correct solution;
- edge cases, error paths and regressions the criteria imply but the tests skip;
- tests that fail for the wrong reason (import error, typo, missing fixture) instead of
  the missing behaviour.

Answer "sound" when the tests are a faithful, sufficient specification; small style points
are not a reason to revise. Answer "revise" only with concrete, actionable `issues`, each
naming the criterion or test it concerns. Do not write the tests yourself."""


def tests_review_prompt(
    brief: str,
    acceptance: str,
    context: dict[str, Any],
    baseline: list[dict[str, Any]],
    diff: str,
    *,
    memory: str = "",
) -> str:
    return assemble(
        TESTS_REVIEW_STATIC,
        _project_layer(memory),
        _run_layer(context),
        _task_layer(brief, acceptance),
        "\n\n".join(
            [
                "## Baseline run (these commands must fail right now)\n" + _render_results(baseline),
                _diff_section(diff),
            ]
        ),
    ).text


tests_review_prompt.__test__ = False


def extract_summary(text: str) -> str:
    """Return the last marked summary, or clipped text when no block is present."""
    summaries = re.findall(r"<summary>(.*?)</summary>", text, re.DOTALL)
    return summaries[-1].strip() if summaries else clip(text, 600)


IMPLEMENT_STATIC = f"""Implement the smallest change that makes the failing tests pass.

Rules:
- Change implementation code, not the tests, unless a test is provably wrong about the stated acceptance \
criteria -- and say so explicitly if you do.
- A bug in tests written for THIS task is in scope: use the tests provably wrong permission above, not a <bug> block.
- Do not disable, skip or loosen assertions to get green.
- Keep the change minimal and consistent with the repo's conventions.
- Alloy owns task tracking, verification and git: do not run `bd`, do not commit, and do not run the whole \
test suite -- run the tests relevant to your change; Alloy runs the full suite when you finish.

Finish with one paragraph inside <summary> and </summary> tags describing what you changed and why, including \
'tests edited: <paths or none>'.

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
            f"## Failed check\n`{check.command}` -> exit {check.exit_code} ({check.headline()})\n\n{check.output_tail}"
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
    filed_text = (
        "\n".join(
            f"- {item.get('bead_id') or '(unfiled)'}: {item.get('title', '')} "
            f"[{item.get('severity', '')}] at {item.get('where') or '?'}"
            for item in filed
        )
        or "(none yet)"
    )
    remediation_text = (
        "\n".join(f"- {item.get('bead_id') or '?'}: {item.get('outcome', '')}" for item in remediations) or "(none yet)"
    )
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
        '- "not-a-bug"    -- the report is the task itself, the tests it was asked to make '
        "pass, or a defect that only appears with this task's changes\n"
        '- "duplicate"    -- it matches a bug already filed in this run (listed above)\n'
        '- "non-blocking" -- a real pre-existing defect the task can finish without; '
        "it is filed for later and stays out of this task's scope\n"
        '- "blocking"     -- a real pre-existing defect the task cannot finish without; '
        "Alloy fixes it autonomously in its own bead before the task continues\n"
        '- "needs-human"  -- it blocks the task but the fix needs an architectural change '
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
    history_text = (
        "\n".join(item if isinstance(item, str) else Attempt.model_validate(item).render() for item in run_history)
        or "(none)"
    )
    remediation_text = (
        "\n".join(f"- {item.get('bead_id') or '?'}: {item.get('outcome', '')}" for item in remediations) or "(none yet)"
    )
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
    return "\n\n".join(
        [
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
            '- "merge"         -- the change is what this defect requires and nothing more; '
            "it fits what the project brief and bead graph say the project is doing\n"
            '- "too-broad"     -- an architecture-sized change for a bug fix: new subsystems, '
            "public API or schema changes, broad refactors, dependency swaps, or work that "
            "belongs to another open bead; judge this against the project brief and the bead "
            "graph, not against a file count\n"
            '- "subverts-task" -- it changes behaviour the parent\'s acceptance criteria rely '
            "on, so the paused task would pass or fail for the wrong reason\n\n"
            "Return verdict, reason and confidence in the required structured output.",
        ]
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
not evidence; say so and do not accept.

Scope: accept minor test-only changes beyond the primary defect file when they only
make other tests honest about ambient configuration (env vars, live routing) and do
not change production behaviour. Do not reject solely because a second test file was
touched."""


def acceptance_prompt(
    acceptance: str,
    diff: str,
    changed_tests: list[str],
    checks_this_iteration: list[dict[str, Any]],
    verifier_stop: dict[str, Any] | None,
    declined: int = 0,
) -> str:
    task = f"## Acceptance criteria\n{acceptance or '(none stated)'}"
    prior = ""
    if declined:
        prior = (
            f"\n\n## Earlier rounds\nYou already asked for more verification {declined} "
            "time(s) and the verifier ran no new check: it judged that re-running would "
            "not change the evidence. Do not ask again for something no command can settle."
        )
    volatile = f"""{_diff_section(diff)}

## Tests changed by the implementer
{chr(10).join(changed_tests) or "(none)"}

## Check evidence (this iteration)
{_render_results(checks_this_iteration, verifier_stop)}{prior}"""
    return assemble(ACCEPTANCE_STATIC, "", "", task, volatile).text


VERIFIER_STATIC = """You are choosing the next verification check for a coding task. You are read-only:
you cannot edit code and you never run anything yourself. Do not modify any file. Alloy runs
the command(s) you name, in the worktree root, exactly as written, and shows you the result.

Answer with the structured output. Either:
- action "run": one or more shell commands Alloy will run in the worktree root. For a single
  check, set `command`, `purpose`, `kind` (regression, targeted, lint, typecheck, build or
  custom -- any project script counts as custom) and `required`. For several checks at once,
  leave `command` empty and list them in `checks` (each entry has command, purpose, kind,
  required). Alloy runs every listed check before asking you again. A red required check
  sends the task straight back to the implementer after a single-check run; after a multi-check
  batch it waits for your stop action. A red optional check is only reported to you.
- action "stop": when the evidence is sufficient. Give the reason and list the
  remaining_risks: only risks that a shell command could check and you did not run.
  Leave out out-of-scope work and concerns no command can settle.

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
    repo_root: Path | None = None,
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
    hints = (
        "## Hints from the repository (not yet verified)\n"
        f"{_render_check_hints(context, changed_files=changed_files, repo_root=repo_root)}"
    )
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
    lessons = "\n".join(f"- {key}: {body}" for key, body in sorted((existing_lessons or {}).items()))
    guidance = human_note.strip()
    volatile = (
        f"{evidence}\n\n"
        + (f"## Human guidance\n{guidance}\n\n" if guidance else "")
        + f"## Existing repository lessons\n{lessons or '(none)'}"
    )
    return assemble(HARVEST_STATIC, "", "", "", volatile).text


MEMORY_REVIEW_STATIC = """You are reviewing project memory for a repository.
You are read-only: do not modify any file and do not run bd.

Every stored memory is listed below with its owner (alloy or human) and, for
alloy-owned entries, the run, bead and date that wrote it. Some carry a
contradiction flag recorded by an earlier run. Deterministic hygiene has
already decided the keys listed under "Already planned"; do not repeat them.

Return one verdict per remaining key:
- "keep" -- still accurate and worth its place in every prompt
- "update" -- keep, but with new_content replacing the body
- "forget" -- stale, wrong, duplicated, or too task-specific to keep
- "embed" -- durable enough to live in the repository's instruction files

Give a one-sentence reason for each. Do not invent keys."""

TREE_SUMMARY_LIMIT = 200
_TREE_SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)


def repo_tree_summary(root: Path, *, limit: int = TREE_SUMMARY_LIMIT) -> str:
    """A bounded, sorted listing of the repository's directories and files
    (relative paths, directories with a trailing slash), skipping VCS and
    tool caches. Stops after ``limit`` entries with a truncation marker."""

    lines: list[str] = []
    stack = [root]
    while stack and len(lines) < limit:
        current = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except OSError:
            continue
        pending: list[Path] = []
        for child in children:
            if child.name in _TREE_SKIP_DIRS or child.name.startswith("."):
                continue
            rel = child.relative_to(root).as_posix()
            if child.is_dir():
                lines.append(rel + "/")
                pending.append(child)
            else:
                lines.append(rel)
            if len(lines) >= limit:
                lines.append("... (truncated)")
                break
        stack.extend(reversed(pending))
    return "\n".join(lines)


def embedded_memory_block(root: Path, instruction_files: list[str]) -> str:
    """The ``## Project memory`` section already embedded in one of the
    repository's instruction files, or "" when none carries one."""

    for name in instruction_files:
        path = root / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.strip() != MEMORY_RENDER_HEADER:
                continue
            section = [line]
            for rest in lines[index + 1 :]:
                if rest.startswith("## "):
                    break
                section.append(rest)
            return "\n".join(section).strip()
    return ""


def memory_review_prompt(
    memory: ProjectMemory,
    planned: ReviewPlan,
    *,
    embedded_block: str,
    tree_summary: str,
) -> str:
    entries = memory.entries
    rows: list[str] = []
    for key in sorted(entries):
        entry = entries[key]
        if entry.owner == "alloy":
            provenance = (
                f"run={entry.run_id or '-'} bead={entry.bead_id or '-'} "
                f"at={entry.date.isoformat() if entry.date else '-'}"
            )
        else:
            provenance = "human-written"
        flag = " [contradiction flagged]" if CONTRADICTION_KEY_PREFIX + key in entries else ""
        rows.append(f"### {key}\nowner: {entry.owner}; {provenance}{flag}\n{entry.body}")
    already = "\n".join(f"- {item.key}: {item.action} -- {item.reason}" for item in planned.items)
    volatile = (
        f"## Memories\n{chr(10).join(rows) or '(none)'}\n\n"
        f"## Already planned\n{already or '(none)'}\n\n"
        f"## Embedded block\n{embedded_block or '(none)'}\n\n"
        f"## Repository tree\n{tree_summary or '(empty)'}"
    )
    return assemble(MEMORY_REVIEW_STATIC, "", "", "", clip(volatile, 12000)).text


def _render_check_hints(
    context: dict[str, Any] | None,
    *,
    changed_files: list[str] | None = None,
    repo_root: Path | None = None,
) -> str:
    """Commands the context role, the bead or autodetection suggested. None of
    them has run; the verifier decides whether any of them is worth running."""
    hints: list[str] = list((context or {}).get("check_hints") or [])
    if changed_files:
        root = repo_root or Path.cwd()
        for path in diff_derived_test_paths(changed_files, root):
            command = f"uv run pytest -n 0 -q {path}"
            if command not in hints:
                hints.append(command)
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


def _render_results(checks: list[dict[str, Any]], verifier_stop: dict[str, Any] | None = None) -> str:
    """The checks of one iteration: every headline, then the last output tail."""
    results = [CheckResult.model_validate(item) for item in checks]
    if results:
        lines = _render_check_lines(results)
        lines += [
            "",
            f"Last output (`{results[-1].command}`):",
            results[-1].output_tail,
        ]
    else:
        lines = ["(no checks were run this iteration)"]
    if verifier_stop:
        stop = VerifierAction.model_validate(verifier_stop)
        lines.append(f"\nVerifier stopped: {stop.reason or '(no reason given)'}")
        if stop.remaining_risks:
            lines.append("Remaining risks: " + "; ".join(stop.remaining_risks))
    return "\n".join(lines)


def _render_history(history: list[dict[str, Any]]) -> str:
    return "\n".join(Attempt.model_validate(item).render() for item in history[-5:])
