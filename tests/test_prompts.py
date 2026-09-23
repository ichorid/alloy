"""alloy-4ef.4: prompts.assemble() and tdd-loop prompt golden tests.

These tests encode the acceptance criteria for five ordered prompt layers joined by
assemble(static, project, run, task, volatile) and for refactoring every tdd-loop
prompt builder onto it. They are expected to fail until the implementation lands.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

from alloy.prompts import LAYER_SEPARATOR, assemble
from alloy.recipes.tdd_loop import (
    BUG_PROTOCOL,
    IMPLEMENT_STATIC,
    JUDGE_STATIC,
    VERIFIER_STATIC,
    _evidence_packet,
    _render_check_hints,
    _render_checks,
    _render_context,
    _render_results,
    _run_layer,
    _task_layer,
    acceptance_prompt,
    context_prompt,
    critic_prompt,
    estimate_prompt,
    implement_prompt,
    judge_prompt,
    synthesize_prompt,
    tests_prompt,
    verifier_prompt,
)
def _load_fake_module():
    path = Path(__file__).parent / "fakebin" / "_fake.py"
    spec = importlib.util.spec_from_file_location("alloy_test_fake", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ROLE_MARKERS = _load_fake_module().ROLE_MARKERS

GOLDEN_DIR = Path(__file__).parent / "golden" / "prompts"

# Distinctive markers for volatile-layer isolation tests (alloy-4ef.6).
MARKER_WORKTREE = "/tmp/alloy-volatile-wt-marker-4ef6"
MARKER_RUN_ID = "run-volatile-marker-4ef6"
MARKER_ITERATION = 77

# Fixed fixture bead shared by every golden comparison.
BRIEF = "# alloy-fixture: Add slugify()\n\nAdd a slugify() helper to mypkg."
BRIEF_B = "# alloy-fixture: Add titlecase()\n\nAdd a titlecase() helper to mypkg."
ACCEPTANCE = "slugify('Hello World') == 'hello-world'"
CONTEXT = {
    "summary": "A small python package with a pytest suite.",
    "relevant_files": ["mypkg/__init__.py"],
    "conventions": ["snake_case"],
    "risks": [],
    "check_hints": ["python -m pytest -q"],
}
RENDERED_CONTEXT = _render_context(CONTEXT)
BASELINE_CHECKS = [
    {
        "command": "python -m pytest -q tests/test_slugify.py",
        "purpose": "Confirm slugify tests fail before implementation",
    }
]
DIFF = (
    "diff --git a/mypkg/__init__.py b/mypkg/__init__.py\n"
    "+++ b/mypkg/__init__.py\n"
    "+def slugify(t): return t\n"
)
CHECK_RESULT = {
    "command": "python -m pytest -q tests/test_slugify.py",
    "exit_code": 1,
    "kind": "targeted",
    "required": True,
    "iteration": 1,
    "output_tail": "FAILED",
}
ATTEMPT_HISTORY = [
    {
        "iteration": 1,
        "implementer": "codex",
        "change_summary": "first attempt",
        "checks": "[targeted] red",
        "decision": "retry",
        "reason": "still failing",
    }
]
CRITIQUES = [
    {
        "critic": "c1",
        "confidence": 0.8,
        "root_cause": "wrong algo",
        "evidence": "test fails",
        "suggested_fix": "fix it",
    }
]
EVIDENCE = "evidence bundle for stuck task"
LIMITS_NOTE = "within limits"

PROMPT_ROLES = (
    "context",
    "estimate",
    "tests",
    "implement",
    "verifier",
    "acceptance",
    "judge",
    "critic",
    "synthesize",
)

TDD_LOOP_ROLE_MARKERS = tuple(
    (role, marker)
    for role, marker in ROLE_MARKERS
    if role in PROMPT_ROLES
)

# Distinctive rule sentences that must survive the refactor (grep list).
RULE_SENTENCES = [
    "If the defect blocks your task, stop and report it.",
    "Never claim to have",
    "run anything yourself",
    "you never run anything yourself",
    "Do not answer \"done\" if tests are failing",
    "Do not answer \"done\" if the diff is empty",
    "The tests must fail right now, because the behavior does not exist yet",
    "do not run the whole test suite -- run only the tests you wrote",
    "do not run the whole test suite -- run the tests relevant to your change",
    "Change implementation code, not the tests, unless a test is provably wrong",
    "Tests that were weakened, skipped or deleted are",
    "not evidence; say so and do not accept",
    "give your own honest reading",
    "Output the instructions as prose, no preamble",
]


def _join_layers(
    static: str = "",
    project: str = "",
    run: str = "",
    task: str = "",
    volatile: str = "",
) -> str:
    return LAYER_SEPARATOR.join(
        layer for layer in (static, project, run, task, volatile) if layer
    )


def _common_prefix(left: str, right: str) -> str:
    index = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        index += 1
    return left[:index]


# Layer bodies for the fixed fixture (target assemble() layout).
_CONTEXT_STATIC = f"""You are gathering context for another agent that will implement this task.
Read the repository. Do not modify any file.

Produce a context packet:
- summary: how this repo is laid out and where this change belongs (<= 300 words)
- relevant_files: paths the implementer will most likely touch or read
- check_hints (optional): commands this repo's files, scripts or CI config suggest
  for running tests, lint or build; a verifier decides what actually runs
- conventions: naming, structure and style rules an outsider would get wrong
- risks: things that could make this change break something else

{BUG_PROTOCOL}"""

_CONTEXT_TASK = f"""{BRIEF}

## Acceptance criteria
{ACCEPTANCE}"""

_ESTIMATE_STATIC = """You are estimating how hard this task is
You are read-only: do not modify any file. Choose one complexity level:
- simple: one file, obvious change, tests are the spec
- medium: a few files or one new concept
- complex: cross-cutting, concurrency, new subsystem, ambiguous acceptance

Return complexity, reason and confidence in the required structured output."""

_ESTIMATE_RUN = f"""## Repository context
{RENDERED_CONTEXT}"""

_ESTIMATE_TASK = f"""{BRIEF}

## Acceptance criteria
{ACCEPTANCE}"""

_TESTS_STATIC = f"""Write failing tests for this task. Do not implement the behavior itself.

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

_TESTS_RUN = f"""## Repository context
{RENDERED_CONTEXT}"""

_TESTS_TASK = f"""{BRIEF}

## Acceptance criteria
{ACCEPTANCE}"""

_IMPLEMENT_STATIC = f"""Implement the smallest change that makes the failing tests pass.

Rules:
- Change implementation code, not the tests, unless a test is provably wrong about the stated acceptance criteria -- and say so explicitly if you do.
- A bug in tests written for THIS task is in scope: use the tests provably wrong permission above, not a <bug> block.
- Do not disable, skip or loosen assertions to get green.
- Keep the change minimal and consistent with the repo's conventions.
- Alloy owns task tracking, verification and git: do not run `bd`, do not commit, and do not run the whole test suite -- run the tests relevant to your change; Alloy runs the full suite when you finish.

Finish with one paragraph inside <summary> and </summary> tags describing what you changed and why, including 'tests edited: <paths or none>'.

{BUG_PROTOCOL}"""

_IMPLEMENT_RUN = f"""## Repository context
{RENDERED_CONTEXT}"""

_IMPLEMENT_TASK = f"""{BRIEF}

## Acceptance criteria
{ACCEPTANCE}

## Checks that must go green
- `python -m pytest -q tests/test_slugify.py` -- Confirm slugify tests fail before implementation"""

_IMPLEMENT_VOLATILE = """## Required changes this iteration
fix slugify"""

_VERIFIER_STATIC = """You are choosing the next verification check for a coding task. You are read-only:
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

_VERIFIER_RUN = f"""## Repository context
{RENDERED_CONTEXT}

## Hints from the repository (not yet verified)
- `python -m pytest -q`"""

_VERIFIER_TASK = f"""{BRIEF}

## Acceptance criteria
{ACCEPTANCE}

## Baseline commands (the tests role's targeted checks; red before implementation)
- `python -m pytest -q tests/test_slugify.py` -- Confirm slugify tests fail before implementation"""

_VERIFIER_VOLATILE = f"""## Changed files
mypkg/__init__.py

## Current diff
```diff
{DIFF}```

## Checks run so far in this run
(none yet)

## Last result
(nothing has run yet this run)

## Acceptance gate
(not consulted yet this iteration)

## Attempt history
#1 via codex: first attempt
   checks: [targeted] red
   judge: retry -- still failing

## Budget
iteration 1; 3 more check(s) allowed this iteration, 5 more in this run"""

_ACCEPTANCE_STATIC = """You are deciding whether there is enough evidence to call a coding task complete.
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

_ACCEPTANCE_TASK = f"""## Acceptance criteria
{ACCEPTANCE}"""

_ACCEPTANCE_VOLATILE = f"""## Current diff
```diff
{DIFF}```

## Tests changed by the implementer
tests/test_slugify.py

## Check evidence (this iteration)
#1 [targeted] `python -m pytest -q tests/test_slugify.py` -> exit 1

Last output (`python -m pytest -q tests/test_slugify.py`):
FAILED"""

_JUDGE_STATIC = """You are judging whether a coding task is complete. You cannot edit code;
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

_JUDGE_RUN = f"""## Repository context
{RENDERED_CONTEXT}"""

_JUDGE_TASK = f"""{BRIEF}

## Acceptance criteria
{ACCEPTANCE}"""

_JUDGE_VOLATILE = f"""## Current diff
```diff
{DIFF}```

## Tests changed by the implementer
(none)

## Test results
#1 [targeted] `python -m pytest -q tests/test_slugify.py` -> exit 1

Last output (`python -m pytest -q tests/test_slugify.py`):
FAILED

## Attempt history
#1 via codex: first attempt
   checks: [targeted] red
   judge: retry -- still failing

## Budget
iteration 1; {LIMITS_NOTE}"""

_CRITIC_STATIC = """You are one of several independent critics reviewing a stuck coding task.
You are read-only: do not modify any file. You cannot see the other critics' opinions,
and that is deliberate -- give your own honest reading.

Identify the single most likely root cause of the failure and the concrete fix.
Be specific about files and behavior. If you believe the tests are wrong rather than
the implementation, say that explicitly."""

_CRITIC_VOLATILE = EVIDENCE

_SYNTHESIZE_STATIC = """Several independent critics reviewed a stuck coding task. Reconcile their
opinions into one instruction set for the implementer. You are read-only.

Where the critics agree, treat it as likely true. Where they disagree, decide which
reading the evidence actually supports and say why. Then write direct, concrete
instructions for the implementer: which files to change and what the change must do.
Output the instructions as prose, no preamble."""

_SYNTHESIZE_VOLATILE = f"""{EVIDENCE}

## Independent opinions
### Critic 1 (c1, confidence 0.8)
root cause: wrong algo
evidence: test fails
suggested fix: fix it"""


def expected_golden(role: str) -> str:
    builders = {
        "context": lambda: _join_layers(_CONTEXT_STATIC, task=_CONTEXT_TASK),
        "estimate": lambda: _join_layers(_ESTIMATE_STATIC, run=_ESTIMATE_RUN, task=_ESTIMATE_TASK),
        "tests": lambda: _join_layers(_TESTS_STATIC, run=_TESTS_RUN, task=_TESTS_TASK),
        "implement": lambda: _join_layers(
            _IMPLEMENT_STATIC,
            run=_IMPLEMENT_RUN,
            task=_IMPLEMENT_TASK,
            volatile=_IMPLEMENT_VOLATILE,
        ),
        "verifier": lambda: _join_layers(
            _VERIFIER_STATIC,
            run=_VERIFIER_RUN,
            task=_VERIFIER_TASK,
            volatile=_VERIFIER_VOLATILE,
        ),
        "acceptance": lambda: _join_layers(
            _ACCEPTANCE_STATIC,
            task=_ACCEPTANCE_TASK,
            volatile=_ACCEPTANCE_VOLATILE,
        ),
        "judge": lambda: _join_layers(
            _JUDGE_STATIC,
            run=_JUDGE_RUN,
            task=_JUDGE_TASK,
            volatile=_JUDGE_VOLATILE,
        ),
        "critic": lambda: _join_layers(_CRITIC_STATIC, volatile=_CRITIC_VOLATILE),
        "synthesize": lambda: _join_layers(_SYNTHESIZE_STATIC, volatile=_SYNTHESIZE_VOLATILE),
    }
    return builders[role]()


def _fixture_prompt(role: str) -> str:
    if role == "context":
        return context_prompt(BRIEF, ACCEPTANCE)
    if role == "estimate":
        return estimate_prompt(BRIEF, ACCEPTANCE, CONTEXT)
    if role == "tests":
        return tests_prompt(BRIEF, ACCEPTANCE, CONTEXT)
    if role == "implement":
        return implement_prompt(
            BRIEF,
            ACCEPTANCE,
            CONTEXT,
            "fix slugify",
            [],
            None,
            BASELINE_CHECKS,
        )
    if role == "verifier":
        return verifier_prompt(
            brief=BRIEF,
            acceptance=ACCEPTANCE,
            context=CONTEXT,
            diff=DIFF,
            changed_files=["mypkg/__init__.py"],
            checks_this_run=[],
            iteration=1,
            checks_left_iteration=3,
            checks_left_run=5,
            history=ATTEMPT_HISTORY,
            baseline_checks=BASELINE_CHECKS,
        )
    if role == "acceptance":
        return acceptance_prompt(
            ACCEPTANCE,
            DIFF,
            ["tests/test_slugify.py"],
            [CHECK_RESULT],
            None,
        )
    if role == "judge":
        return judge_prompt(
            BRIEF,
            ACCEPTANCE,
            CONTEXT,
            DIFF,
            [CHECK_RESULT],
            ATTEMPT_HISTORY,
            1,
            LIMITS_NOTE,
        )
    if role == "critic":
        return critic_prompt(EVIDENCE)
    if role == "synthesize":
        return synthesize_prompt(EVIDENCE, CRITIQUES)
    raise KeyError(role)


def _implement_prompt_iteration_two() -> str:
    return implement_prompt(
        BRIEF,
        ACCEPTANCE,
        CONTEXT,
        "fix again",
        ATTEMPT_HISTORY,
        CHECK_RESULT,
        BASELINE_CHECKS,
        diff=DIFF,
        previous_instructions="try harder",
    )


def _verifier_prompt_check_two() -> str:
    return verifier_prompt(
        brief=BRIEF,
        acceptance=ACCEPTANCE,
        context=CONTEXT,
        diff=DIFF,
        changed_files=["mypkg/__init__.py"],
        checks_this_run=[CHECK_RESULT],
        iteration=1,
        checks_left_iteration=2,
        checks_left_run=4,
        history=ATTEMPT_HISTORY,
        baseline_checks=BASELINE_CHECKS,
    )


# ---------------------------------------------------------------------------
# alloy.prompts.assemble()
# ---------------------------------------------------------------------------


def test_assemble_module_exports_join_helper():
    from alloy.prompts import assemble

    assert assemble("alpha", "", "beta", "", "gamma").text == f"alpha{LAYER_SEPARATOR}beta{LAYER_SEPARATOR}gamma"


def test_assemble_skips_empty_layers():
    from alloy.prompts import assemble

    assert assemble("", "project", "", "task", "").text == f"project{LAYER_SEPARATOR}task"


def test_assemble_returns_empty_string_when_all_layers_empty():
    from alloy.prompts import assemble

    assert assemble("", "", "", "", "").text == ""


def test_assemble_layer_separator_matches_test_contract():
    from alloy.prompts import LAYER_SEPARATOR as PROMPT_SEPARATOR

    assert PROMPT_SEPARATOR == LAYER_SEPARATOR


# ---------------------------------------------------------------------------
# Golden prompt fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", PROMPT_ROLES)
def test_role_prompt_matches_golden_file(role: str):
    golden = (GOLDEN_DIR / f"{role}.txt").read_text(encoding="utf-8")
    assert _fixture_prompt(role) == golden


def test_golden_files_use_layer_separator_between_sections():
    for role in PROMPT_ROLES:
        golden = (GOLDEN_DIR / f"{role}.txt").read_text(encoding="utf-8")
        layer_count = sum(
            1
            for layer in (
                globals().get(f"_{role.upper()}_STATIC"),
                globals().get(f"_{role.upper()}_PROJECT"),
                globals().get(f"_{role.upper()}_RUN"),
                globals().get(f"_{role.upper()}_TASK"),
                globals().get(f"_{role.upper()}_VOLATILE"),
            )
            if layer
        )
        if layer_count > 1:
            assert LAYER_SEPARATOR in golden


# ---------------------------------------------------------------------------
# Prefix stability across beads, iterations, and verifier checks
# ---------------------------------------------------------------------------


def test_implement_prompts_for_two_beads_share_static_prefix():
    prompt_a = implement_prompt(
        BRIEF,
        ACCEPTANCE,
        CONTEXT,
        "fix slugify",
        [],
        None,
        BASELINE_CHECKS,
    )
    prompt_b = implement_prompt(
        BRIEF_B,
        ACCEPTANCE,
        CONTEXT,
        "fix slugify",
        [],
        None,
        BASELINE_CHECKS,
    )
    static_prefix = _join_layers(_IMPLEMENT_STATIC)
    shared = _common_prefix(prompt_a, prompt_b)
    assert len(shared) >= len(static_prefix)
    assert prompt_a.startswith(static_prefix)
    assert prompt_b.startswith(static_prefix)


def test_implement_prompt_iterations_share_prefix_through_task_layer():
    prompt_one = _fixture_prompt("implement")
    prompt_two = _implement_prompt_iteration_two()
    task_prefix = _join_layers(_IMPLEMENT_STATIC, run=_IMPLEMENT_RUN, task=_IMPLEMENT_TASK)
    shared = _common_prefix(prompt_one, prompt_two)
    assert len(shared) >= len(task_prefix)
    assert prompt_one.startswith(task_prefix)
    assert prompt_two.startswith(task_prefix)
    assert BRIEF in task_prefix


def test_verifier_prompt_consecutive_checks_share_prefix_through_task_layer():
    prompt_one = _fixture_prompt("verifier")
    prompt_two = _verifier_prompt_check_two()
    task_prefix = _join_layers(_VERIFIER_STATIC, run=_VERIFIER_RUN, task=_VERIFIER_TASK)
    shared = _common_prefix(prompt_one, prompt_two)
    assert len(shared) >= len(task_prefix)
    assert prompt_one.startswith(task_prefix)
    assert prompt_two.startswith(task_prefix)
    assert BRIEF in task_prefix


# ---------------------------------------------------------------------------
# Rule sentences and fake-harness role markers
# ---------------------------------------------------------------------------


def test_rule_sentences_preserved_in_golden_files():
    golden_text = "\n".join(
        (GOLDEN_DIR / f"{role}.txt").read_text(encoding="utf-8") for role in PROMPT_ROLES
    )
    missing = [sentence for sentence in RULE_SENTENCES if sentence not in golden_text]
    assert missing == []


@pytest.mark.parametrize("role,marker", TDD_LOOP_ROLE_MARKERS)
def test_role_marker_matches_prompt_first_line(role: str, marker: str):
    prompt = _fixture_prompt(role)
    first_line = prompt.splitlines()[0]
    assert marker in first_line


# ---------------------------------------------------------------------------
# alloy-4ef.6: prefix_hash and volatile-layer isolation
# ---------------------------------------------------------------------------


def test_assemble_returns_prefix_hash_over_static_project_run_layers():
    """prefix_hash is sha256 over the joined static, project and run layers."""
    static, project, run = "static-body", "project-body", "run-body"
    task, volatile = "task-body", "volatile-body"

    text, prefix_hash = assemble(static, project, run, task, volatile)

    joined = LAYER_SEPARATOR.join((static, project, run))
    assert prefix_hash == hashlib.sha256(joined.encode("utf-8")).hexdigest()
    assert text == LAYER_SEPARATOR.join((static, project, run, task, volatile))


def _implement_stable_layers(brief: str, acceptance: str, baseline_checks: list[dict]) -> str:
    task_parts = [_task_layer(brief, acceptance)]
    if baseline_checks:
        rendered = _render_checks(baseline_checks)
        if rendered:
            task_parts.append(f"## Checks that must go green\n{rendered}")
    return assemble(IMPLEMENT_STATIC, "", _run_layer(CONTEXT), "\n\n".join(task_parts), "").text


def _assert_markers_absent_from_stable_prefix(stable: str) -> None:
    for marker in (MARKER_WORKTREE, MARKER_RUN_ID, str(MARKER_ITERATION)):
        assert marker not in stable


def test_judge_prompt_excludes_volatile_markers_from_stable_layers():
    check_with_markers = {
        **CHECK_RESULT,
        "iteration": MARKER_ITERATION,
        "duration_s": 123.4,
        "timed_out": True,
        "log_path": f"/logs/{MARKER_RUN_ID}/check-1.log",
        "output_tail": f"failure rooted at {MARKER_WORKTREE}",
    }
    stable = assemble(
        JUDGE_STATIC, "", _run_layer(CONTEXT), _task_layer(BRIEF, ACCEPTANCE), ""
    ).text
    prompt = judge_prompt(
        BRIEF,
        ACCEPTANCE,
        CONTEXT,
        DIFF,
        [check_with_markers],
        ATTEMPT_HISTORY,
        MARKER_ITERATION,
        LIMITS_NOTE,
    )

    assert prompt.startswith(stable + LAYER_SEPARATOR)
    _assert_markers_absent_from_stable_prefix(stable)


def test_verifier_prompt_excludes_volatile_markers_from_stable_layers():
    check_with_markers = {
        **CHECK_RESULT,
        "iteration": MARKER_ITERATION,
        "duration_s": 456.7,
        "timed_out": True,
        "log_path": f"/logs/{MARKER_RUN_ID}/verifier-check.log",
        "output_tail": f"stderr mentions {MARKER_WORKTREE}",
    }
    hints = f"## Hints from the repository (not yet verified)\n{_render_check_hints(CONTEXT)}"
    baseline = (
        "## Baseline commands (the tests role's targeted checks; red before implementation)\n"
        f"{_render_checks(BASELINE_CHECKS)}"
    )
    stable = assemble(
        VERIFIER_STATIC,
        "",
        f"{_run_layer(CONTEXT)}\n\n{hints}",
        f"{_task_layer(BRIEF, ACCEPTANCE)}\n\n{baseline}",
        "",
    ).text
    prompt = verifier_prompt(
        brief=BRIEF,
        acceptance=ACCEPTANCE,
        context=CONTEXT,
        diff=DIFF,
        changed_files=[f"mypkg/__init__.py", MARKER_WORKTREE],
        checks_this_run=[check_with_markers],
        iteration=MARKER_ITERATION,
        checks_left_iteration=2,
        checks_left_run=4,
        history=ATTEMPT_HISTORY,
        baseline_checks=BASELINE_CHECKS,
    )

    assert prompt.startswith(stable + LAYER_SEPARATOR)
    _assert_markers_absent_from_stable_prefix(stable)


def test_render_results_omits_log_paths_and_durations_from_check_lines():
    """Check headlines rendered for prompts must not embed log paths or durations."""
    check_with_markers = {
        **CHECK_RESULT,
        "duration_s": 123.4,
        "timed_out": True,
        "log_path": f"/logs/{MARKER_RUN_ID}/check-1.log",
    }
    rendered = _render_results([check_with_markers], None)

    assert MARKER_RUN_ID not in rendered
    assert "123" not in rendered
    assert "duration" not in rendered.lower()


def test_implement_repair_prompt_excludes_volatile_markers_from_stable_layers():
    failed_check = {
        **CHECK_RESULT,
        "iteration": MARKER_ITERATION,
        "duration_s": 89.1,
        "log_path": f"/logs/{MARKER_RUN_ID}/repair.log",
        "output_tail": f"trace references {MARKER_WORKTREE}",
    }
    stable = _implement_stable_layers(BRIEF, ACCEPTANCE, BASELINE_CHECKS)
    prompt = implement_prompt(
        BRIEF,
        ACCEPTANCE,
        CONTEXT,
        f"repair using run {MARKER_RUN_ID}",
        ATTEMPT_HISTORY,
        failed_check,
        BASELINE_CHECKS,
        diff=DIFF,
        previous_instructions=f"inspect {MARKER_WORKTREE}",
    )

    assert prompt.startswith(stable + LAYER_SEPARATOR)
    _assert_markers_absent_from_stable_prefix(stable)


def test_evidence_packet_matches_layer_joined_golden():
    """_evidence_packet is refactored onto assemble(); pin its fixture output too."""

    class _Bead:
        def task_brief(self) -> str:
            return BRIEF

        @property
        def acceptance_criteria(self) -> str:
            return ACCEPTANCE

    class _Ctx:
        bead = _Bead()

    state = {
        "context": CONTEXT,
        "iteration": 1,
        "checks": [CHECK_RESULT],
        "attempts": ATTEMPT_HISTORY,
        "verifier_stop": None,
    }
    golden = (GOLDEN_DIR / "evidence_packet.txt").read_text(encoding="utf-8")
    actual = _evidence_packet(state, _Ctx(), DIFF)
    assert actual == golden
