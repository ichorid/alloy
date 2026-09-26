"""Checkpoint state contracts for the TDD recipe."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class TddState(TypedDict, total=False):
    bead_id: str
    run_id: str
    title: str

    context: dict[str, Any]
    memory_block: str  # rendered project memory, fixed at run start
    memory_check_hints: str  # stored alloy:check-hints body, fixed at run start
    memory_calibration: str  # stored alloy:calibration body, fixed at run start
    memory_keys: list[str]  # every stored memory key, fixed at run start
    memory_lessons: dict[str, str]  # stored alloy:lesson:* bodies, fixed at run start
    memory_regressions: dict[str, str]  # refreshed after an unmerged remediation
    complexity: str
    complexity_source: str
    retries_on_tier: int
    escalations: Annotated[list[dict[str, Any]], operator.add]
    baseline_checks: list[dict[str, Any]]  # CheckRequest dicts from the tests role
    baseline: list[dict[str, Any]] | None  # CheckResult dicts from prove_red
    baseline_repairs: int
    tests_reviews: int  # independent reviews of the tests so far
    tests_session: dict[str, Any] | None  # {runner, session_id} of the last tests call
    test_fingerprints: dict[str, str]  # path -> sha256 of every test file after prove_red

    iteration: int
    consiliums: int
    instructions: str
    implementer: str  # the runner that actually produced the last change

    checks: Annotated[list[dict[str, Any]], operator.add]  # CheckResult dicts, every iteration
    iteration_checks: int  # checks run in the current iteration
    verifier_stop: dict[str, Any] | None  # the VerifierAction that ended the last loop
    last_check: dict[str, Any] | None  # the most recent CheckResult
    last_instructions: str  # what the last implement call was told
    pending_check: dict[str, Any] | None  # the VerifierAction run_check_step executes
    unrunnable_streak: int  # consecutive checks that could not run
    verify_more_at_checks: int | None  # len(checks) when acceptance last said verify_more
    verify_more_declined: int  # verify_more rounds asked this iteration
    verify_route: str | None  # where verifier_step / run_check_step sent the run
    acceptance: dict[str, Any] | None  # the AcceptanceVerdict after post-processing
    acceptance_route: str | None  # where acceptance_gate sent the run
    decision: dict[str, Any] | None
    change_summary: str
    attempts: Annotated[list[dict[str, Any]], operator.add]
    reported_bugs: Annotated[list[dict[str, Any]], operator.add]
    triaged_titles: list[str]  # reports the triage role has labelled
    filed_bugs: list[dict[str, Any]]  # {bead_id, title, where, severity}
    implementer_stopped: bool  # the last implement call reported blocks_task yes
    blocking_bug: dict[str, Any] | None  # the bug currently routed to remediate
    remediations: Annotated[list[dict[str, Any]], operator.add]  # {bead_id, outcome}, by remediate
    triage_route: str | None  # where the last triage sent the run
    critiques: Annotated[list[dict[str, Any]], reset_or_extend]
    budget_extensions: int

    stage: str
    outcome: str | None
    outcome_reason: str
    conflict_files: list[str]  # land: paths the trial merge conflicted on
    limit_hit: str | None
    human_note: str
    implement_unavailable: bool  # implement's whole fallback chain was unavailable
    resume_to: str | None  # stage that parked the run at the human gate
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


def reset_or_extend(
    current: list[dict[str, Any]] | None, incoming: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Append critiques as they arrive; a None update clears the round."""
    if incoming is None:
        return []
    return [*(current or []), *incoming]
