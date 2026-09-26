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

import asyncio as asyncio
from datetime import date as date
from typing import Any as Any

from langgraph.graph import END as END
from langgraph.graph import START as START
from langgraph.graph import StateGraph as StateGraph
from langgraph.types import Send as Send
from langgraph.types import interrupt as interrupt

from alloy.beads import LABEL_BUG as LABEL_BUG
from alloy.beads import LABEL_HUMAN as LABEL_HUMAN
from alloy.beads import META_DISCOVERED_IN_RUN as META_DISCOVERED_IN_RUN
from alloy.beads import META_RECIPE as META_RECIPE
from alloy.config import RoleSpec as RoleSpec
from alloy.memory_embed import is_block_stale as is_block_stale
from alloy.memory_embed import last_review_date as last_review_date
from alloy.models import CALIBRATION_KEY as CALIBRATION_KEY
from alloy.models import CHECK_HINTS_KEY as CHECK_HINTS_KEY
from alloy.models import CONTRADICTION_KEY_PREFIX as CONTRADICTION_KEY_PREFIX
from alloy.models import EMBED_STALE_KEY as EMBED_STALE_KEY
from alloy.models import LESSON_KEY_PREFIX as LESSON_KEY_PREFIX
from alloy.models import REGRESSION_KEY_PREFIX as REGRESSION_KEY_PREFIX
from alloy.models import AgentResult as AgentResult
from alloy.models import Attempt as Attempt
from alloy.models import BugReport as BugReport
from alloy.models import BugTriage as BugTriage
from alloy.models import CheckRequest as CheckRequest
from alloy.models import CheckResult as CheckResult
from alloy.models import ComplexityEstimate as ComplexityEstimate
from alloy.models import ContextPacket as ContextPacket
from alloy.models import Critique as Critique
from alloy.models import HarvestAnswer as HarvestAnswer
from alloy.models import JudgeDecision as JudgeDecision
from alloy.models import Outcome as Outcome
from alloy.models import ProjectMemory as ProjectMemory
from alloy.models import ReviewPlan as ReviewPlan
from alloy.models import ReviewVerdicts as ReviewVerdicts
from alloy.models import TestsOutput as TestsOutput
from alloy.models import TestsReview as TestsReview
from alloy.models import clip as clip
from alloy.models import extract_bug_reports as extract_bug_reports
from alloy.models import format_calibration as format_calibration
from alloy.models import format_check_hints as format_check_hints
from alloy.models import is_unavailable as is_unavailable
from alloy.models import memory_hygiene as memory_hygiene
from alloy.models import merge_review_plan as merge_review_plan
from alloy.models import next_level as next_level
from alloy.models import parse_check_hints as parse_check_hints
from alloy.models import update_calibration as update_calibration
from alloy.models import utcnow as utcnow
from alloy.models import with_provenance as with_provenance
from alloy.recipes.bug_triage import TriageFailure as TriageFailure
from alloy.recipes.bug_triage import _diffstat as _diffstat
from alloy.recipes.bug_triage import bug_acceptance as bug_acceptance
from alloy.recipes.bug_triage import bug_description as bug_description
from alloy.recipes.bug_triage import remediation_call_estimate as remediation_call_estimate
from alloy.recipes.bug_triage import scope_gate as scope_gate
from alloy.recipes.bug_triage import scope_merge_gate as scope_merge_gate
from alloy.recipes.bug_triage import triage_reports as triage_reports
from alloy.recipes.bug_triage import untriaged as untriaged
from alloy.recipes.role_prompts import _TREE_SKIP_DIRS as _TREE_SKIP_DIRS
from alloy.recipes.role_prompts import ACCEPTANCE_STATIC as ACCEPTANCE_STATIC
from alloy.recipes.role_prompts import BEAD_DESIGN_OUTRANKS_MEMORY as BEAD_DESIGN_OUTRANKS_MEMORY
from alloy.recipes.role_prompts import BUG_PROTOCOL as BUG_PROTOCOL
from alloy.recipes.role_prompts import CONTEXT_SCHEMA as CONTEXT_SCHEMA
from alloy.recipes.role_prompts import CONTEXT_STATIC as CONTEXT_STATIC
from alloy.recipes.role_prompts import CRITIC_STATIC as CRITIC_STATIC
from alloy.recipes.role_prompts import ESTIMATE_STATIC as ESTIMATE_STATIC
from alloy.recipes.role_prompts import HARVEST_STATIC as HARVEST_STATIC
from alloy.recipes.role_prompts import IMPLEMENT_STATIC as IMPLEMENT_STATIC
from alloy.recipes.role_prompts import JUDGE_STATIC as JUDGE_STATIC
from alloy.recipes.role_prompts import MAX_DIFF_CHARS as MAX_DIFF_CHARS
from alloy.recipes.role_prompts import MEMORY_REVIEW_STATIC as MEMORY_REVIEW_STATIC
from alloy.recipes.role_prompts import PER_FILE_DIFF_CHARS as PER_FILE_DIFF_CHARS
from alloy.recipes.role_prompts import PROJECT_CONTEXT_CHARS as PROJECT_CONTEXT_CHARS
from alloy.recipes.role_prompts import REMEDIATION_MIN_AGENT_CALLS as REMEDIATION_MIN_AGENT_CALLS
from alloy.recipes.role_prompts import SYNTHESIZE_STATIC as SYNTHESIZE_STATIC
from alloy.recipes.role_prompts import TESTS_RESUMED as TESTS_RESUMED
from alloy.recipes.role_prompts import TESTS_REVIEW_STATIC as TESTS_REVIEW_STATIC
from alloy.recipes.role_prompts import TESTS_STATIC as TESTS_STATIC
from alloy.recipes.role_prompts import TREE_SUMMARY_LIMIT as TREE_SUMMARY_LIMIT
from alloy.recipes.role_prompts import VERIFIER_RESUMED as VERIFIER_RESUMED
from alloy.recipes.role_prompts import VERIFIER_STATIC as VERIFIER_STATIC
from alloy.recipes.role_prompts import _diff_section as _diff_section
from alloy.recipes.role_prompts import _project_layer as _project_layer
from alloy.recipes.role_prompts import _render_check_hints as _render_check_hints
from alloy.recipes.role_prompts import _render_check_lines as _render_check_lines
from alloy.recipes.role_prompts import _render_checks as _render_checks
from alloy.recipes.role_prompts import _render_context as _render_context
from alloy.recipes.role_prompts import _render_history as _render_history
from alloy.recipes.role_prompts import _render_results as _render_results
from alloy.recipes.role_prompts import _run_layer as _run_layer
from alloy.recipes.role_prompts import _task_layer as _task_layer
from alloy.recipes.role_prompts import acceptance_prompt as acceptance_prompt
from alloy.recipes.role_prompts import clip_diff as clip_diff
from alloy.recipes.role_prompts import context_prompt as context_prompt
from alloy.recipes.role_prompts import critic_prompt as critic_prompt
from alloy.recipes.role_prompts import embedded_memory_block as embedded_memory_block
from alloy.recipes.role_prompts import estimate_prompt as estimate_prompt
from alloy.recipes.role_prompts import extract_summary as extract_summary
from alloy.recipes.role_prompts import harvest_prompt as harvest_prompt
from alloy.recipes.role_prompts import implement_prompt as implement_prompt
from alloy.recipes.role_prompts import judge_prompt as judge_prompt
from alloy.recipes.role_prompts import log as log
from alloy.recipes.role_prompts import memory_review_prompt as memory_review_prompt
from alloy.recipes.role_prompts import render_project_context as render_project_context
from alloy.recipes.role_prompts import repo_tree_summary as repo_tree_summary
from alloy.recipes.role_prompts import scope_prompt as scope_prompt
from alloy.recipes.role_prompts import synthesize_prompt as synthesize_prompt
from alloy.recipes.role_prompts import tests_prompt as tests_prompt
from alloy.recipes.role_prompts import tests_review_prompt as tests_review_prompt
from alloy.recipes.role_prompts import triage_prompt as triage_prompt
from alloy.recipes.role_prompts import verifier_prompt as verifier_prompt
from alloy.recipes.shared_verification import VerifyLoop as VerifyLoop
from alloy.recipes.shared_verification import _check_hints as _check_hints
from alloy.recipes.shared_verification import _checks_headline as _checks_headline
from alloy.recipes.shared_verification import _checks_of as _checks_of
from alloy.recipes.shared_verification import _decision_from as _decision_from
from alloy.recipes.shared_verification import _evidence_packet as _evidence_packet
from alloy.recipes.shared_verification import _implementer_changed_tests as _implementer_changed_tests
from alloy.recipes.shared_verification import _is_red as _is_red
from alloy.recipes.shared_verification import _last_check_summary as _last_check_summary
from alloy.recipes.shared_verification import _retry_at_iso as _retry_at_iso
from alloy.recipes.shared_verification import _unavailable_decision as _unavailable_decision
from alloy.recipes.shared_verification import answer_of as answer_of
from alloy.recipes.shared_verification import call_in_session as call_in_session
from alloy.recipes.shared_verification import classify as classify
from alloy.recipes.shared_verification import make_verify_loop as make_verify_loop
from alloy.recipes.shared_verification import resumable_session as resumable_session
from alloy.recipes.shared_verification import was_resumed as was_resumed
from alloy.recipes.state import CriticInput as CriticInput
from alloy.recipes.state import TddState as TddState
from alloy.recipes.state import reset_or_extend as reset_or_extend
from alloy.recipes.workflow_nodes import _context_from as _context_from
from alloy.recipes.workflow_nodes import _make_node__capture_bugs as _make_node__capture_bugs
from alloy.recipes.workflow_nodes import _make_node__dispatch_critics as _make_node__dispatch_critics
from alloy.recipes.workflow_nodes import _make_node__file_bug as _make_node__file_bug
from alloy.recipes.workflow_nodes import _make_node__first_available as _make_node__first_available
from alloy.recipes.workflow_nodes import _make_node__park as _make_node__park
from alloy.recipes.workflow_nodes import _make_node_critic as _make_node_critic
from alloy.recipes.workflow_nodes import _make_node_estimate as _make_node_estimate
from alloy.recipes.workflow_nodes import _make_node_finish as _make_node_finish
from alloy.recipes.workflow_nodes import _make_node_gather_context as _make_node_gather_context
from alloy.recipes.workflow_nodes import _make_node_guard as _make_node_guard
from alloy.recipes.workflow_nodes import _make_node_harvest as _make_node_harvest
from alloy.recipes.workflow_nodes import _make_node_human_gate as _make_node_human_gate
from alloy.recipes.workflow_nodes import _make_node_implement as _make_node_implement
from alloy.recipes.workflow_nodes import _make_node_prove_red as _make_node_prove_red
from alloy.recipes.workflow_nodes import (
    _make_node_record_memory_contradictions as _make_node_record_memory_contradictions,
)
from alloy.recipes.workflow_nodes import _make_node_remediate as _make_node_remediate
from alloy.recipes.workflow_nodes import _make_node_remember_calibration as _make_node_remember_calibration
from alloy.recipes.workflow_nodes import _make_node_remember_check_hints as _make_node_remember_check_hints
from alloy.recipes.workflow_nodes import _make_node_remember_lesson as _make_node_remember_lesson
from alloy.recipes.workflow_nodes import _make_node_review_tests as _make_node_review_tests
from alloy.recipes.workflow_nodes import _make_node_route as _make_node_route
from alloy.recipes.workflow_nodes import _make_node_route_after_human as _make_node_route_after_human
from alloy.recipes.workflow_nodes import _make_node_route_after_implement as _make_node_route_after_implement
from alloy.recipes.workflow_nodes import _make_node_route_after_prove_red as _make_node_route_after_prove_red
from alloy.recipes.workflow_nodes import _make_node_route_after_remediate as _make_node_route_after_remediate
from alloy.recipes.workflow_nodes import _make_node_route_after_review as _make_node_route_after_review
from alloy.recipes.workflow_nodes import _make_node_route_after_tests as _make_node_route_after_tests
from alloy.recipes.workflow_nodes import _make_node_route_after_triage as _make_node_route_after_triage
from alloy.recipes.workflow_nodes import _make_node_synthesize as _make_node_synthesize
from alloy.recipes.workflow_nodes import _make_node_triage as _make_node_triage
from alloy.recipes.workflow_nodes import _make_node_write_tests as _make_node_write_tests
from alloy.recipes.workflow_nodes import _tests_output_from as _tests_output_from
from alloy.runtime import RunContext as RunContext
from alloy.worktree import is_test_path as is_test_path

# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------


async def review_memory(ctx: RunContext, memory: ProjectMemory, *, today: date) -> ReviewPlan:
    """Plan a memory review: deterministic hygiene, then the memory_reviewer
    role's verdicts on the remaining known keys. Never writes to bd."""

    spec = ctx.recipe.memory
    hygiene = memory_hygiene(memory, spec.ttl_days, today)
    planned = ReviewPlan(items=hygiene)
    prompt = memory_review_prompt(
        memory,
        planned,
        embedded_block=embedded_memory_block(ctx.worktree.path, spec.instruction_files),
        tree_summary=repo_tree_summary(ctx.worktree.path),
    )
    default = ReviewVerdicts(verdicts=[])
    answer = await classify(
        ctx,
        "memory_reviewer",
        ctx.recipe.role("memory_reviewer"),
        prompt,
        model_cls=ReviewVerdicts,
        default=default,
    )
    if answer is default:
        log.warning("memory_reviewer: %s; plan is hygiene only", default.reason)
        return merge_review_plan(hygiene, None, set(memory.entries), reviewer_reason=default.reason)
    return merge_review_plan(hygiene, answer, set(memory.entries))


def existing_lessons(memory: ProjectMemory | None) -> dict[str, str]:
    """The live alloy:lesson:* entries, key -> provenance-stripped body."""
    if memory is None:
        return {}
    return {key: entry.body for key, entry in memory.entries.items() if key.startswith(LESSON_KEY_PREFIX)}


# --------------------------------------------------------------------------
# rendering helpers -- these are what keep state small
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# graph
# --------------------------------------------------------------------------


def build_graph(ctx: RunContext, *, skip_context: bool = False):
    """Compile the tdd-loop graph bound to one task's runtime.

    ``skip_context`` drops the context-gathering phase entirely (no
    "context" node, no role lookup): estimate runs first, off of START,
    and every prompt that reads ``state.get("context", {})`` sees the
    empty dict it already tolerates.
    """

    _capture_bugs = _make_node__capture_bugs(ctx)
    record_memory_contradictions = _make_node_record_memory_contradictions(ctx)
    estimate = _make_node_estimate(ctx)
    write_tests = _make_node_write_tests(ctx, _capture_bugs)
    route_after_tests = _make_node_route_after_tests()
    route_after_human = _make_node_route_after_human()
    prove_red = _make_node_prove_red(ctx)
    route_after_prove_red = _make_node_route_after_prove_red(ctx)
    route_after_review = _make_node_route_after_review()
    implement = _make_node_implement(ctx, _capture_bugs)
    route_after_implement = _make_node_route_after_implement()
    _first_available = _make_node__first_available(ctx)
    _park = _make_node__park()
    _file_bug = _make_node__file_bug(ctx)
    triage = _make_node_triage(ctx, _first_available, _park, _file_bug)
    route_after_triage = _make_node_route_after_triage()
    remediate = _make_node_remediate(ctx)
    route_after_remediate = _make_node_route_after_remediate()
    guard = _make_node_guard(ctx)
    _dispatch_critics = _make_node__dispatch_critics(ctx)
    critic = _make_node_critic(ctx)
    synthesize = _make_node_synthesize(ctx)
    human_gate = _make_node_human_gate(ctx)
    remember_check_hints = _make_node_remember_check_hints(ctx)
    remember_calibration = _make_node_remember_calibration(ctx)
    remember_lesson = _make_node_remember_lesson(ctx)
    gather_context = _make_node_gather_context(ctx, _capture_bugs, record_memory_contradictions)
    review_tests = _make_node_review_tests(ctx, _first_available)
    route = _make_node_route(_dispatch_critics)
    finish = _make_node_finish(ctx, remember_check_hints, remember_calibration)
    harvest = _make_node_harvest(ctx, remember_lesson)

    verify = make_verify_loop(ctx)

    graph = StateGraph(TddState)
    if not skip_context:
        graph.add_node("context", gather_context)
    graph.add_node("estimate", estimate)
    graph.add_node("tests", write_tests)
    graph.add_node("prove_red", prove_red)
    graph.add_node("tests_review", review_tests)
    graph.add_node("implement", implement)
    graph.add_node("triage", triage)
    graph.add_node("remediate", remediate)
    graph.add_node("verifier_step", verify.verifier_step)
    graph.add_node("run_check_step", verify.run_check_step)
    graph.add_node("acceptance_gate", verify.acceptance_gate)
    graph.add_node("judge", verify.judge)
    graph.add_node("guard", guard)
    graph.add_node("critic", critic, input_schema=CriticInput)
    graph.add_node("synthesize", synthesize)
    graph.add_node("human_gate", human_gate)
    graph.add_node("finish", finish)
    graph.add_node("harvest", harvest)

    if skip_context:
        graph.add_edge(START, "estimate")
    else:
        graph.add_edge(START, "context")
        graph.add_edge("context", "estimate")
    graph.add_edge("estimate", "tests")
    graph.add_conditional_edges("tests", route_after_tests, ["prove_red", "finish", "human_gate"])
    graph.add_conditional_edges(
        "prove_red",
        route_after_prove_red,
        ["tests", "tests_review", "implement", "human_gate"],
    )
    graph.add_conditional_edges("tests_review", route_after_review, ["tests", "implement"])
    graph.add_conditional_edges("implement", route_after_implement, ["triage", "verifier_step", "human_gate"])
    graph.add_conditional_edges(
        "triage",
        route_after_triage,
        ["remediate", "human_gate", "implement", "verifier_step"],
    )
    graph.add_conditional_edges("remediate", route_after_remediate, ["implement", "human_gate"])
    graph.add_conditional_edges(
        "verifier_step",
        verify.route_after_verifier,
        ["run_check_step", "acceptance_gate", "guard", "human_gate"],
    )
    graph.add_conditional_edges(
        "run_check_step",
        verify.route_after_check,
        ["verifier_step", "guard", "human_gate"],
    )
    graph.add_conditional_edges(
        "acceptance_gate",
        verify.route_after_acceptance,
        ["guard", "verifier_step", "judge"],
    )
    graph.add_edge("judge", "guard")
    graph.add_conditional_edges("guard", route, ["implement", "critic", "human_gate", "finish"])
    graph.add_edge("critic", "synthesize")
    graph.add_edge("synthesize", "implement")
    graph.add_conditional_edges("human_gate", route_after_human, ["implement", "tests"])
    graph.add_edge("finish", "harvest")
    graph.add_edge("harvest", END)

    return graph.compile(checkpointer=ctx.checkpointer)


def initial_state(ctx: RunContext) -> TddState:
    memory = ctx.project_memory()
    flag_stale_embed_block(ctx, memory)
    return TddState(
        bead_id=ctx.bead.id,
        run_id=ctx.run_id,
        title=ctx.bead.title,
        memory_block=memory.render() if memory is not None else "",
        memory_check_hints=memory.body_of(CHECK_HINTS_KEY) if memory is not None else "",
        memory_calibration=memory.body_of(CALIBRATION_KEY) if memory is not None else "",
        memory_keys=sorted(memory.entries) if memory is not None else [],
        memory_lessons=existing_lessons(memory),
        memory_regressions={
            key: entry.body for key, entry in memory.entries.items() if key.startswith(REGRESSION_KEY_PREFIX)
        }
        if memory is not None
        else {},
        iteration=0,
        consiliums=0,
        retries_on_tier=0,
        escalations=[],
        instructions="",
        baseline_checks=[],
        baseline=None,
        baseline_repairs=0,
        tests_reviews=0,
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
        verify_more_at_checks=None,
        verify_more_declined=0,
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


def flag_stale_embed_block(ctx: RunContext, memory: ProjectMemory | None) -> None:
    """Run start (alloy-4ef.20): when a managed embed block in the worktree's
    instruction files has drifted from the run-start memory snapshot or
    outlived its review, set ``alloy:meta:embed-stale=true`` once and leave
    one note on the bead. Files without a block are skipped; the flag itself
    carries no provenance because the scheduler forgets it after the review."""
    if memory is None or ctx.beads is None or not ctx.recipe.memory.enabled:
        return
    spec = ctx.recipe.memory
    reviewed = last_review_date(memory)
    stale = [
        name
        for name in spec.instruction_files
        if (path := ctx.worktree.path / name).is_file()
        and is_block_stale(path.read_text(encoding="utf-8"), memory, reviewed, spec)
    ]
    if not stale:
        return
    try:
        ctx.beads.remember(EMBED_STALE_KEY, "true")
    except Exception:
        log.warning("could not remember %s", EMBED_STALE_KEY, exc_info=True)
        return
    ctx.beads.note(
        ctx.bead.id,
        f"alloy: run {ctx.run_id} found a stale managed memory block in "
        + ", ".join(stale)
        + f"; set {EMBED_STALE_KEY}=true for review",
    )
