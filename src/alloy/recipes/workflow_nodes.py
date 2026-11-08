"""Node behavior for the TDD workflow graph."""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.types import Send, interrupt

from alloy.beads import LABEL_BUG, LABEL_HUMAN, LABEL_NEEDS_SPEC, META_DISCOVERED_IN_RUN, META_REFINED
from alloy.config import RoleSpec
from alloy.models import (
    CALIBRATION_KEY,
    CHECK_HINTS_KEY,
    CONTRADICTION_KEY_PREFIX,
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
    RefinedSpec,
    TestsOutput,
    TestsReview,
    clip,
    extract_bug_reports,
    format_calibration,
    format_check_hints,
    is_unavailable,
    next_level,
    parse_check_hints,
    update_calibration,
    utcnow,
    with_provenance,
)
from alloy.recipes.bug_triage import (
    bug_acceptance,
    bug_description,
    triage_reports,
    untriaged,
)
from alloy.recipes.known_red import excused_note, failing_tests, failure_key, read_log
from alloy.recipes.role_prompts import (
    CONTEXT_SCHEMA,
    context_prompt,
    critic_prompt,
    estimate_prompt,
    extract_summary,
    harvest_prompt,
    implement_prompt,
    log,
    refine_prompt,
    synthesize_prompt,
    tests_prompt,
    tests_review_prompt,
)
from alloy.recipes.shared_verification import (
    ACCEPTANCE_REPAIR_PREFIX,
    VERIFY_MORE_BREACH_PREFIX,
    _check_hints,
    _check_target,
    _checks_headline,
    _checks_of,
    _evidence_packet,
    _implementer_changed_tests,
    _is_red,
    _last_check_summary,
    _latest_per_target,
    _retry_at_iso,
    _unavailable_decision,
    call_in_session,
    call_on_branch,
    classify,
    limit_gate,
    limit_refused,
    limit_stop,
    resumable_session,
)
from alloy.recipes.state import CriticInput, TddState
from alloy.worktree import is_test_path, tree_fingerprint

BUDGET_LIMITS: tuple[str, ...] = (
    "max_agent_calls",
    "max_cheap_agent_calls",
    "max_iterations",
    "max_wall_time",
    "max_total_checks",
)
"""Breaches a budget-stop auto-land may follow. A consilium stop says
something about the work, not just the clock. `max_total_checks` is one since
the bench calibration (2026-11-07): finished work with every check green was
parked on it; `budget_landing`'s green-regression-on-an-unchanged-tree rule
still keeps a verifier spinning on red checks from landing."""

EXHAUSTION_NEARLY_DONE = "nearly-done"
EXHAUSTION_STALLED = "stalled"
EXHAUSTION_INFRASTRUCTURE = "infrastructure"
EXHAUSTION_PROGRESSING = "progressing"


def red_signature(state: TddState, iteration: int) -> set[str]:
    """What is still red at the end of `iteration`: the failing test ids its
    red required checks report (pytest, dart), else the check target itself.
    Test ids, not commands, so a verifier that narrows `pytest tests/x.py` to
    one node id between iterations does not look like progress."""
    red: set[str] = set()
    for check in _latest_per_target(_checks_of(state, iteration)):
        if not _is_red(check):
            continue
        result = CheckResult.model_validate(check)
        excused = set(result.excused)
        tests = {failure_key(test) for test in failing_tests(read_log(result))} - excused
        red |= tests or {_check_target(result.command)}
    return red


def stalled(state: TddState, iterations: int = 1) -> bool:
    """Nothing red got fixed over the last `iterations` iterations: each
    iteration's red signature contains the one before it."""
    iteration = state.get("iteration", 0)
    later = red_signature(state, iteration)
    for back in range(1, iterations + 1):
        earlier = red_signature(state, iteration - back)
        if not earlier or not earlier <= later:
            return False
        later = earlier
    return True


def classify_exhaustion(state: TddState, decision: JudgeDecision) -> str:
    """Why a limit stopped this run, from its checks alone:

    - infrastructure: a check of the final iteration could not run, or a
      runner was unavailable;
    - nearly-done: every target of the final iteration is green, so only the
      evidence or the verdict was missing;
    - stalled: everything red in the previous iteration is still red;
    - progressing: something got fixed, something is still red.
    """
    latest = _latest_per_target(_checks_of(state, state.get("iteration", 0)))
    if any(not CheckResult.model_validate(check).runnable for check in latest) or "unavailable" in decision.reason:
        return EXHAUSTION_INFRASTRUCTURE
    if latest and not any(_is_red(check) for check in latest):
        return EXHAUSTION_NEARLY_DONE
    if stalled(state):
        return EXHAUSTION_STALLED
    return EXHAUSTION_PROGRESSING


STALL_ITERATIONS = 2
"""Iterations in a row that fixed nothing before guard turns a retry into a
consilium (while the consilium budget lasts)."""


def _stall_consilium(ctx, state: TddState, decision: JudgeDecision, recorded: dict[str, Any]) -> JudgeDecision:
    """A retry after `STALL_ITERATIONS` iterations that fixed nothing becomes
    a consilium, while the consilium budget lasts and a critic is available:
    fresh critics read the evidence instead of the same implementer trying a
    third time (bench calibration: cheap implementers repeat one failing
    approach until max_iterations). Returns `decision` unchanged otherwise;
    a stall is journalled through guard's `recorded` update."""
    if decision.decision != "retry" or not stalled(state, STALL_ITERATIONS):
        return decision
    if state.get("consiliums", 0) >= ctx.recipe.limits.max_consiliums * ctx.budget(state):
        return decision
    if not ctx.available_critics():
        return decision
    red = sorted(red_signature(state, state.get("iteration", 0)))
    sample = ", ".join(red[:3]) + (f" and {len(red) - 3} more" if len(red) > 3 else "")
    stall = JudgeDecision(
        decision="consilium",
        reason=(
            f"Alloy: {sample} stayed red for {STALL_ITERATIONS + 1} iterations; asking the critics "
            f"instead of another retry. Last reason: {decision.reason}"
        ),
        next_instructions=decision.next_instructions,
        confidence=decision.confidence,
    )
    recorded["journal"] = [f"iteration {state.get('iteration', 0)}: {stall.reason}"]
    return stall


def _make_node__capture_bugs(ctx):
    def _capture_bugs(role: str, result: AgentResult, state: TddState, iteration: int) -> list[dict[str, Any]]:
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
                        f"alloy: {role} reported bug '{bug.title}' at {bug.where} (blocks_task={bug.blocks_task})",
                    )
                except Exception:
                    log.warning("could not record bug on bead %s", ctx.bead.id, exc_info=True)
        return reports

    return _capture_bugs


def _make_node_gather_context(ctx, _capture_bugs, record_memory_contradictions):
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
            packet.check_hints,
            ctx.bead.check_hint,
            (ctx.worktree.path, ctx.worktrees.repo),
            memory_hints=parse_check_hints(state.get("memory_check_hints", "")),
        )
        record_memory_contradictions(packet.compact()["memory_contradictions"], state.get("memory_keys") or [])
        return {
            "reported_bugs": reported_bugs,
            "context": packet.compact(),
            "stage": "context",
        }

    return gather_context


def _make_node_record_memory_contradictions(ctx):
    def record_memory_contradictions(contradictions: list[str], memory_keys: list[str]) -> None:
        """Flag each ``key: why`` the context role reported against an existing
        project memory as alloy:review:contradiction:<key> for a reviewer, and
        leave one note on the bead. Entries for keys outside the run-start
        memory snapshot are dropped; the disputed memory itself is never
        modified or deleted."""
        if not contradictions or ctx.beads is None or not ctx.recipe.memory.enabled:
            return
        known = set(memory_keys)
        flagged: list[str] = []
        for entry in contradictions:
            key, sep, why = entry.partition(": ")
            if not sep:
                key, sep, why = entry.partition(":")
            key, why = key.strip(), why.strip()
            if not sep or not key or key not in known or key in flagged:
                continue
            body = f"{key}: {why}" if why else key
            try:
                ctx.beads.remember(
                    f"{CONTRADICTION_KEY_PREFIX}{key}",
                    with_provenance(body, ctx.run_id, ctx.bead.id, utcnow().date()),
                )
            except Exception:
                log.warning("could not remember contradiction for %s", key, exc_info=True)
                continue
            flagged.append(key)
        if flagged:
            ctx.beads.note(
                ctx.bead.id,
                f"alloy: run {ctx.run_id} context flagged memory contradiction(s) for review: "
                + ", ".join(f"{CONTRADICTION_KEY_PREFIX}{key}" for key in flagged),
            )

    return record_memory_contradictions


REFINED_HEADING = "## Refined by Alloy"


def refined_acceptance(original: str, answer: RefinedSpec) -> str:
    """The operator's acceptance criteria with the refine role's additions
    below them, under a heading that says who wrote them."""
    lines = [REFINED_HEADING, "Testable criteria added for behaviour the description names:"]
    lines += [f"- {criterion}" for criterion in answer.criteria]
    if answer.assumptions:
        lines += ["", "Assumptions made where the description is vague:"]
        lines += [f"- {assumption}" for assumption in answer.assumptions]
    return "\n".join([original.rstrip(), "", *lines]).strip()


OTHER_TASKS_SHOWN = 30


def _other_open_tasks(ctx) -> list[str]:
    """ "id: title" of the open tasks besides this bead (no epics, no Alloy
    bugs), same epic first, so refine leaves their behaviour to them (bench
    full-4/toy-7: copy gained cross-sheet criteria before sheets existed)."""
    if ctx.beads is None:
        return []
    try:
        rows = ctx.beads.all_rows()
        root = ctx.beads.epic_root(ctx.bead.id)
    except Exception:
        log.warning("%s: could not list the other open tasks for refine", ctx.bead.id, exc_info=True)
        return []
    others = [
        row
        for row in rows
        if row.get("id") != ctx.bead.id
        and row.get("status") not in ("closed", "tombstone")
        and row.get("issue_type") != "epic"
        and LABEL_BUG not in (row.get("labels") or [])
    ]
    same_epic = {bead.id for bead in ctx.beads.open_descendants(root)} if root else set()
    others.sort(key=lambda row: (row.get("id") not in same_epic, row.get("priority", 2)))
    return [f"{row.get('id')}: {row.get('title', '')}" for row in others[:OTHER_TASKS_SHOWN]]


def _make_node_refine(ctx):
    async def refine(state: TddState) -> dict[str, Any]:
        """Bench calibration (D8): a bead whose description names five
        behaviours and whose acceptance names one closed done with only that
        one built. The optional `refine` role turns the description into
        testable criteria plus recorded assumptions, appended to the bead's
        acceptance in bd (so a resumed run and every later role see them).
        Once per bead (`alloy_refined`); alloy-bug beads are already specific.
        "unclear" only labels the bead `alloy-needs-spec` and the run goes on."""
        spec = ctx.recipe.roles.get("refine")
        if spec is None or ctx.bead.metadata.get(META_REFINED) or LABEL_BUG in ctx.bead.labels:
            return {}
        ctx.set_stage("refine")
        default = RefinedSpec(decision="unchanged")
        answer = await classify(
            ctx,
            "refine",
            spec,
            refine_prompt(
                ctx.bead.task_brief(),
                ctx.bead.acceptance_criteria,
                state.get("context", {}),
                memory=state.get("memory_block", ""),
                other_tasks=_other_open_tasks(ctx),
            ),
            model_cls=RefinedSpec,
            default=default,
        )
        if answer is default:
            return {"stage": "refine", "journal": [f"refine: no answer ({answer.reason}); criteria unchanged"]}
        journal = f"refine: {answer.decision}"
        if answer.decision == "refined" and answer.criteria:
            text = refined_acceptance(ctx.bead.acceptance_criteria, answer)
            ctx.bead = ctx.bead.model_copy(update={"acceptance_criteria": text})
            _best_effort(ctx, lambda beads: beads.set_acceptance(ctx.bead.id, text))
            journal += f" -- {len(answer.criteria)} criteria, {len(answer.assumptions)} assumptions added"
        elif answer.decision == "unclear":
            _best_effort(ctx, lambda beads: beads.add_label(ctx.bead.id, LABEL_NEEDS_SPEC))
            _best_effort(
                ctx, lambda beads: beads.note(ctx.bead.id, f"alloy: refine found no testable goal: {answer.reason}")
            )
            journal += f" -- {answer.reason}"
        _best_effort(ctx, lambda beads: beads.set_metadata(ctx.bead.id, {META_REFINED: answer.decision}))
        return {"stage": "refine", "refined": answer.model_dump(), "journal": [journal]}

    return refine


def _best_effort(ctx, action) -> None:
    if ctx.beads is None:
        return
    try:
        action(ctx.beads)
    except Exception as exc:  # the in-memory bead already carries the change
        log.warning("%s: refine could not update bd: %s", ctx.bead.id, exc)


def _make_node_estimate(ctx):
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
                    calibration=format_calibration(state.get("memory_calibration", "")),
                ),
                model_cls=ComplexityEstimate,
                default=default,
            )
            level = answer.complexity
            source = "default" if answer is default else "estimate"
            reason = f"{answer.reason} (confidence {answer.confidence:.2f})"
        ctx.set_complexity(level, source, reason)
        return {"complexity": level, "complexity_source": source, "stage": "estimate"}

    return estimate


def _make_node_write_tests(ctx, _capture_bugs):
    async def write_tests(state: TddState) -> dict[str, Any]:
        ctx.set_stage("tests")
        breach = limit_gate(ctx, state, include_iterations=False)
        if breach:
            return _tests_limit_park(breach)
        spec = ctx.role_spec("tests", state)
        # A repair pass (green or unrunnable baseline) continues the session
        # that wrote the tests; the first pass has no session yet.
        session_id = resumable_session(spec, state.get("tests_session"))
        prompt_args = (
            ctx.task_brief(),
            ctx.bead.acceptance_criteria,
            state.get("context", {}),
            state.get("instructions", ""),
        )
        prompt_kwargs = dict(memory=state.get("memory_block", ""))
        result = await call_in_session(
            ctx,
            "tests",
            spec,
            tests_prompt(*prompt_args, **prompt_kwargs),
            tests_prompt(*prompt_args, **prompt_kwargs, resumed=True),
            session_id=session_id,
            schema=TestsOutput.schema_for_agents(),
            iteration=0,
        )
        reported_bugs = _capture_bugs("tests", result, state, 0)
        if limit_refused(result):
            return {"reported_bugs": reported_bugs, **_tests_limit_park(result.usage.get("limit") or result.error)}
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
            # Every tests pass (first or repair) is a new revision: branches
            # forked from an older one (implementer, verifier) are re-forked.
            previous = state.get("tests_session") or {}
            update["tests_session"] = {
                "runner": result.runner,
                "model": result.model,
                "session_id": result.session_id,
                "revision": int(previous.get("revision") or 0) + 1,
            }
        return update

    return write_tests


def _tests_limit_park(breach: str) -> dict[str, Any]:
    """The tests stage has no guard downstream of it: a limit breach parks
    the run at the human gate directly, resumable at the tests stage."""
    log.warning("limit gate tests: %s", breach)
    return {
        "stage": "tests",
        "resume_to": "tests",
        "limit_hit": breach,
        "decision": JudgeDecision(
            decision="human",
            reason=f"Alloy stopped the loop: {breach} (before the tests stage)",
            next_instructions="Resume to grant one more budget window; the tests stage runs again.",
        ).model_dump(),
    }


def _make_node_route_after_tests():
    def route_after_tests(state: TddState) -> str:
        """A harness that cannot write the tests has nothing for the implementer
        to aim at: pause for a human, who decides whether to retry or cancel."""
        decision = (state.get("decision") or {}).get("decision")
        if decision == "abort":
            return "finish"
        if decision == "human":
            return "human_gate"
        return "prove_red"

    return route_after_tests


def _make_node_route_after_human():
    def route_after_human(state: TddState) -> str:
        return state.get("resume_target") or "implement"

    return route_after_human


def _make_node_prove_red(ctx):
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
            if not result.runnable or result.phantom_paths:
                # A baseline red only because its command names files that do
                # not exist would hand the implementer a target that can never
                # go green.
                problems.append(f"`{result.command}`: {result.headline()}")
                unrunnable = True
            elif result.ok:
                problems.append(f"`{result.command}`: passed")
        if not problems:
            # The implementer starts from here: remember what every test file
            # looked like so the gates can tell its edits from the tests role's.
            test_paths = [path for path in ctx.worktrees.changed_files(ctx.worktree) if is_test_path(path)]
            update["test_fingerprints"] = ctx.worktrees.fingerprints(ctx.worktree, test_paths)
            return update
        repairs = state.get("baseline_repairs", 0) + 1
        headline = "baseline not runnable" if unrunnable else "baseline unexpectedly green"
        detail = f"{headline}: " + "; ".join(problems)
        if repairs > ctx.recipe.verification.max_baseline_repairs:
            question = (
                "The tests role could not produce a red, runnable baseline. Tell it what to fix; "
                "the tests stage runs again."
            )
            if not checks:
                # Not rerouted automatically: a malformed tests-role answer
                # also arrives as an empty baseline_checks, and it is
                # indistinguishable here from a deliberate "nothing to prove
                # red" (tentura-brd4.18 and two more, one overnight session).
                hint = f"`alloy reroute {ctx.bead.id} fast-track` if nothing new needs proving red"
                detail = f"{detail} -- {hint}"
                question = (
                    "The tests role named no baseline command. If this bead has no new behaviour "
                    "to prove red (an audit, a characterization, an already-fixed or one-line "
                    f"fix), run `alloy reroute {ctx.bead.id} fast-track`: it cancels this run and "
                    "the scheduler redispatches the bead on fast-track, which needs no baseline. "
                    "Otherwise tell the tests role what to prove red; the tests stage runs again."
                )
            update.update(
                resume_to="tests",
                decision=JudgeDecision(
                    decision="human",
                    reason=detail,
                    next_instructions=question,
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

    return prove_red


def _make_node_route_after_prove_red(ctx):
    def route_after_prove_red(state: TddState) -> str:
        if (state.get("decision") or {}).get("decision") == "human" and state.get("resume_to"):
            return "human_gate"
        if state.get("instructions"):
            return "tests"
        if (
            "tests_review" in ctx.recipe.roles
            and state.get("tests_reviews", 0) < ctx.recipe.verification.max_test_reviews
        ):
            return "tests_review"
        return "implement"

    return route_after_prove_red


def _make_node_review_tests(ctx, _first_available):
    async def review_tests(state: TddState) -> dict[str, Any]:
        """An independent model checks the red tests against the task before any
        code is written. Advisory: an unavailable or failing reviewer never blocks."""
        reviews = state.get("tests_reviews", 0) + 1
        ctx.set_stage("tests_review")
        update: dict[str, Any] = {"stage": "tests_review", "tests_reviews": reviews}
        if limit_gate(ctx, state, include_iterations=False):
            # Advisory: skip it; implement's own gate parks the run.
            return update
        role_spec = ctx.recipe.role("tests_review")
        prompt = tests_review_prompt(
            ctx.task_brief(),
            ctx.bead.acceptance_criteria,
            state.get("context", {}),
            state.get("baseline") or [],
            ctx.diff(),
            memory=state.get("memory_block", ""),
        )

        async def review_with(spec: RoleSpec) -> TestsReview | None:
            failure = TestsReview(verdict="sound")
            review = await classify(
                ctx,
                "tests_review",
                spec,
                prompt,
                model_cls=TestsReview,
                default=failure,
                iteration=0,
            )
            return None if review is failure else review

        members = [m for m in role_spec.panel if ctx.registry.available(m.runner)]
        if members:
            # A panel: every available member reviews independently. One member
            # failing or missing just narrows it; only when none answers does the
            # role's own fallback run.
            answers = [r for r in await asyncio.gather(*(review_with(m) for m in members)) if r is not None]
            if not answers and role_spec.fallback is not None:
                spec = _first_available(role_spec.fallback)
                answers = [r for r in [await review_with(spec) if spec else None] if r]
            issues = list(dict.fromkeys(i for r in answers if r.verdict != "sound" for i in r.issues))
        else:
            spec = (
                _first_available(role_spec)
                if not role_spec.panel
                else (_first_available(role_spec.fallback) if role_spec.fallback else None)
            )
            if spec is None:
                return update
            review = await review_with(spec)
            issues = review.issues if review and review.verdict != "sound" else []
        if not issues:
            return update
        update["instructions"] = (
            "An independent reviewer read your tests against the acceptance criteria and "
            "found problems. Fix them (or, if a point is wrong, keep the test and be sure "
            "it is right), then answer with the corrected baseline_checks:\n"
            + "\n".join(f"- {issue}" for issue in issues)
        )
        return update

    return review_tests


def _make_node_route_after_review():
    def route_after_review(state: TddState) -> str:
        return "tests" if state.get("instructions") else "implement"

    return route_after_review


def _make_node_implement(ctx, _capture_bugs):
    async def implement(state: TddState) -> dict[str, Any]:
        iteration = state.get("iteration", 0) + 1
        # Every edge into implement -- guard's retry, triage's bug repair,
        # synthesize, a human resume, the tests review -- starts a new
        # iteration, so the full limits (max_iterations included) are
        # checked here, before the agent is called.
        breach = limit_gate(ctx, state, include_iterations=True)
        if breach:
            ctx.set_stage("implement", iteration=state.get("iteration", 0))
            return {
                "stage": "implement",
                "implement_unavailable": False,
                **limit_stop(
                    breach,
                    f"before implement iteration {iteration}",
                    next_instructions=state.get("instructions", ""),
                ),
            }
        ctx.set_stage("implement", iteration=iteration)
        spec = ctx.role_spec("implement", state)
        live_tiered = ctx.recipe.complexity.routing == "live" and ctx.recipe.role("implement").tiered
        if live_tiered:
            ctx.set_dispatch_tier(state.get("complexity"))
        last_check = state.get("last_check")
        repairing = bool(last_check) and _is_red(last_check)
        prompt_args = (
            ctx.task_brief(),
            ctx.bead.acceptance_criteria,
            state.get("context", {}),
            state.get("instructions", ""),
            state.get("attempts", []),
            last_check if repairing else None,
        )
        prompt_kwargs = dict(
            baseline_checks=state.get("baseline_checks", []),
            diff=ctx.diff() if repairing else "",
            previous_instructions=state.get("last_instructions", "") if repairing else "",
            memory=state.get("memory_block", ""),
            not_ours=excused_note(state.get("known_red") or {}, ctx.worktree.base_commit),
        )
        # The tests writer implements, in its own branch of its session.
        result, implement_session = await call_on_branch(
            ctx,
            "implement",
            spec,
            implement_prompt(*prompt_args, **prompt_kwargs),
            implement_prompt(*prompt_args, **prompt_kwargs, resumed=True),
            own=state.get("implement_session"),
            # Live complexity routing picks the implementer per tier; that
            # choice wins over branching the tests writer's session.
            tests_session=None if live_tiered else state.get("tests_session"),
            read_only=False,
            iteration=iteration,
        )
        reported_bugs = _capture_bugs("implement", result, state, iteration)
        if limit_refused(result):
            return {
                "reported_bugs": reported_bugs,
                "iteration": iteration,
                "stage": "implement",
                "implement_unavailable": False,
                **limit_stop(
                    result.usage.get("limit") or result.error or "max_wall_time reached",
                    f"during implement iteration {iteration}",
                    next_instructions=state.get("instructions", ""),
                ),
            }
        if is_unavailable(result):
            return {
                "reported_bugs": reported_bugs,
                "iteration": iteration,
                "stage": "implement",
                "resume_to": "implement",
                "implement_unavailable": True,
                "decision": _unavailable_decision("implement", result).model_dump(),
            }
        return {
            "implement_session": implement_session,
            "implement_unavailable": False,
            "limit_breach": None,
            "reported_bugs": reported_bugs,
            "implementer_stopped": any(bug.get("blocks_task") for bug in reported_bugs),
            "iteration": iteration,
            "iteration_checks": 0,
            "unrunnable_streak": 0,
            "verify_more_at_checks": None,
            "verify_more_declined": 0,
            "last_instructions": state.get("instructions", ""),
            "stage": "implement",
            "instructions": "",
            "implementer": result.runner,
            "change_summary": extract_summary(result.text) if result.ok else result.summary,
        }

    return implement


def _make_node_route_after_implement():
    def route_after_implement(state: TddState) -> str:
        if state.get("implement_unavailable"):
            return "human_gate"
        if state.get("limit_breach"):
            return "guard"
        return "triage" if untriaged(state) else "verifier_step"

    return route_after_implement


def _make_node__first_available(ctx):
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

    return _first_available


def _make_node__park():
    def _park(reason: str, question: str) -> dict[str, Any]:
        return {
            "triage_route": "human_gate",
            "resume_to": "implement",
            "decision": JudgeDecision(decision="human", reason=reason, next_instructions=question).model_dump(),
        }

    return _park


def _make_node__file_bug(ctx):
    def _file_bug(report: BugReport, verdict: BugTriage) -> str:
        if ctx.beads is None:
            return "(unfiled)"
        severity = verdict.severity
        labels = [LABEL_BUG] + ([LABEL_HUMAN] if severity == "needs-human" else [])
        # No alloy_recipe copied from the parent: a pinned recipe outranks the
        # scheduler's --recipe and alloy:default:recipe, and dispatch stamps
        # one on every parent, so a one-line bug found by a tdd-loop run was
        # forced through tdd-loop and parked on "no baseline command given".
        # Unassigned, it takes the session default (fast-track by fallback).
        metadata = {META_DISCOVERED_IN_RUN: ctx.run_id}
        bug_id = ctx.beads.create_bug(
            title=report.title,
            description=bug_description(report, verdict, ctx.bead.id, ctx.run_id),
            acceptance=bug_acceptance(report),
            discovered_from=ctx.bead.id,
            priority=3 if severity == "non-blocking" else 1,
            labels=labels,
            metadata=metadata,
            # Nothing ever dispatches a blocking bug separately any more --
            # it is folded into this same run's instructions/journal -- so
            # the bead is never claimed, just filed for the record.
            claim=False,
        )
        if severity == "needs-human":
            ctx.beads.add_dependency(ctx.bead.id, bug_id)
        return bug_id

    return _file_bug


def _make_node_triage(ctx, _first_available, _park, _file_bug):
    async def triage(state: TddState) -> dict[str, Any]:
        return await triage_reports(state, ctx, _first_available, _park, _file_bug)

    return triage


def _make_node_route_after_triage():
    def route_after_triage(state: TddState) -> str:
        return state.get("triage_route") or "verifier_step"

    return route_after_triage


def _guard_decision(proposed, tests_green, ctx):
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
            next_instructions=proposed.next_instructions or "The worktree has no changes; implement the task.",
            confidence=proposed.confidence,
        )
    return decision


def _landable_acceptance(breach: str, decision: JudgeDecision, acceptance: dict[str, Any]) -> str | None:
    """Which acceptance ask a budget stop may land over: `verify_more`, a
    repair beyond met criteria (`criteria_met`), or None."""
    if not breach.startswith(BUDGET_LIMITS) or decision.decision != "retry":
        return None
    if decision.reason.startswith(VERIFY_MORE_BREACH_PREFIX) and acceptance.get("decision") == "verify_more":
        return "verify_more"
    if (
        decision.reason.startswith(ACCEPTANCE_REPAIR_PREFIX)
        and acceptance.get("decision") == "repair"
        and acceptance.get("criteria_met") is True
    ):
        return "criteria_met"
    return None


def budget_landing(ctx, state: TddState, breach: str, decision: JudgeDecision) -> dict[str, Any] | None:
    """Whether a budget-stopped run may finish as done anyway, and the audit
    record for it; None keeps today's needs-human park.

    Deliberately conservative -- every condition must hold:
    - the stop is a pure budget limit (`BUDGET_LIMITS`);
    - the last word was the acceptance gate asking for *more* verification
      (`VERIFY_MORE_BREACH_PREFIX`), or asking for a repair while saying every
      listed criterion is met (`criteria_met`: the demand goes beyond the
      criteria and is filed as a follow-up bug) -- never a failing check or a
      judge's verdict (whose free text may name a defect);
    - the latest `regression` check (the broad one) passed with exit 0;
    - no check of the final iteration failed;
    - the worktree is non-empty and unchanged since that regression ran.
    """
    acceptance = state.get("acceptance") or {}
    kind = _landable_acceptance(breach, decision, acceptance)
    if kind is None:
        return None
    checks = [CheckResult.model_validate(item) for item in state.get("checks") or []]
    regressions = [check for check in checks if check.kind == "regression"]
    if not regressions:
        return None
    regression = regressions[-1]
    if not ((regression.ok and regression.exit_code == 0) or regression.all_excused) or not regression.fingerprint:
        return None
    final = [CheckResult.model_validate(item) for item in _checks_of(state, state.get("iteration", 0))]
    if any(not check.ok and not check.all_excused for check in final):
        return None
    if not ctx.worktrees.has_changes(ctx.worktree):
        return None
    if tree_fingerprint(ctx.worktree.path) != regression.fingerprint:
        return None
    green = list(dict.fromkeys(check.command for check in [*final, regression]))
    landing = {
        "limit": breach,
        "regression": regression.command,
        "green_checks": green,
        "acceptance_reason": acceptance.get("reason", ""),
    }
    if kind == "criteria_met":
        landing["kind"] = kind
    return landing


def _budget_landed_update(ctx, state: TddState, breach: str, decision: JudgeDecision) -> dict[str, Any] | None:
    """guard's update for a budget stop that `budget_landing` lets finish as done."""
    landing = budget_landing(ctx, state, breach, decision)
    if landing is None:
        return None
    if landing.get("kind") == "criteria_met":
        landing["follow_up"] = _file_follow_up(ctx, landing["acceptance_reason"])
    log.warning(
        "%s: budget-landed -- %s, but the regression check `%s` and every final check are green "
        "on an unchanged tree; finishing as done and marking the bead budget-landed",
        ctx.bead.id,
        breach,
        landing["regression"],
    )
    return {
        "stage": "guard",
        "limit_hit": breach,
        "budget_landed": landing,
        # The regression check already is the broad final pass.
        "final_pass": True,
        "decision": JudgeDecision(
            decision="done",
            reason=(
                f"budget-landed: {breach}, but every check is green on an unchanged tree "
                f"(regression `{landing['regression']}` passed). Last judge reason: {decision.reason}"
            ),
            confidence=decision.confidence,
        ).model_dump(),
    }


def _file_follow_up(ctx, request: str) -> str | None:
    """A P3 alloy-bug for what the acceptance gate asked beyond the bead's
    criteria (bench full-6/toy-2: every criterion green, five iterations spent
    on an extra overflow edge case, twelve dependents blocked). Best effort."""
    if ctx.beads is None:
        return None
    try:
        return ctx.beads.create_bug(
            title=f"Follow-up from {ctx.bead.id}: {ctx.bead.title}",
            description=(
                f"The acceptance gate asked for this beyond {ctx.bead.id}'s acceptance criteria, which "
                f"were all met when the run's budget ran out:\n\n{request}"
            ),
            acceptance=request,
            discovered_from=ctx.bead.id,
            priority=3,
            labels=[LABEL_BUG],
            metadata={META_DISCOVERED_IN_RUN: ctx.run_id},
            claim=False,
        )
    except Exception as exc:
        log.warning("%s: could not file the follow-up bug: %s", ctx.bead.id, exc)
        return None


def _breach_update(ctx, state: TddState, breach: str, decision: JudgeDecision) -> dict[str, Any]:
    """guard's update once a limit is breached: a budget-landed finish when
    `budget_landing` allows it, else the needs-human park."""
    landed = _budget_landed_update(ctx, state, breach, decision)
    if landed is not None:
        return landed
    cause = classify_exhaustion(state, decision)
    return {
        "stage": "guard",
        "limit_hit": breach,
        "exhaustion": cause,
        "decision": JudgeDecision(
            decision="human",
            reason=f"Alloy stopped the loop ({cause}): {breach}. Last judge reason: {decision.reason}",
            next_instructions=decision.next_instructions,
            confidence=decision.confidence,
        ).model_dump(),
    }


def _judge_verify_update(state: TddState, decision: JudgeDecision, iteration: int) -> dict[str, Any] | None:
    """The judge said the code is right but required checks never ran on this
    tree: send the run back to the verifier with the judge's commands, not to
    a new implement iteration (2026-11-07: 10 of 21 tentura judge retries were
    exactly this, each a full implement round). Once per iteration -- a second
    `verify` in the same iteration returns None and becomes a retry -- and the
    iteration gets a fresh per-iteration check budget for it
    (`max_total_checks` still bounds the run)."""
    if state.get("judge_verify_iteration") == iteration:
        return None
    instructions = decision.next_instructions or decision.reason
    return {
        "stage": "guard",
        "decision": decision.model_dump(),
        "judge_verify_iteration": iteration,
        "instructions": f"The judge found the diff complete but these checks missing on this tree: {instructions}",
        "journal": [f"iteration {iteration}: the judge asked for checks only, no code change -- {decision.reason}"],
        "verifier_stop": None,
        "pending_check": None,
        "verify_route": None,
        "unrunnable_streak": 0,
        "iteration_checks": 0,
    }


def _make_node_guard(ctx):
    def guard(state: TddState) -> dict[str, Any]:
        """The deterministic gate.

        Every limit is enforced here and nowhere else, so there is exactly one
        place to read to know how the loop can end. The judge's decision is a
        proposal; what comes out of this function is what actually happens.
        """
        proposed = JudgeDecision.model_validate(
            state.get("decision") or {"decision": "retry", "reason": "no decision recorded"}
        )
        # A node's limit gate may have stopped on a breach (wall time spent
        # just before a call); guard always honours it.
        breach = state.get("limit_breach") or ctx.check_limits(state)
        # Green means: every required target checked this iteration passed on
        # its latest run.
        tests_green = not any(
            _is_red(check) for check in _latest_per_target(_checks_of(state, state.get("iteration", 0)))
        )

        decision = _guard_decision(proposed, tests_green, ctx)

        iteration = state.get("iteration", 0)
        recorded: dict[str, Any] = {}
        if not any(row.get("iteration") == iteration for row in state.get("attempts") or []):
            # The iteration ends here without a judge or repair row (e.g. the
            # acceptance gate accepted): record it once, with guard's verdict.
            recorded["attempts"] = [
                Attempt(
                    iteration=iteration,
                    implementer=state.get("implementer") or ctx.role_spec("implement", state).runner,
                    change_summary=state.get("change_summary", ""),
                    checks=_checks_headline(_checks_of(state, iteration)),
                    decision=decision.decision,
                    reason=decision.reason,
                    changed_tests=_implementer_changed_tests(ctx, state),
                ).model_dump()
            ]

        if decision.decision in ("done", "abort", "human"):
            return {"stage": "guard", "decision": decision.model_dump(), **recorded}

        if decision.decision == "verify":
            verify_update = _judge_verify_update(state, decision, iteration)
            if verify_update is not None and not breach:
                return {**recorded, **verify_update}
            decision = JudgeDecision(
                decision="retry",
                reason=f"{decision.reason}; "
                + ("a run limit is spent" if breach else "the judge already asked for checks this iteration")
                + ", so this is a retry",
                next_instructions=decision.next_instructions,
                confidence=decision.confidence,
            )

        if breach:
            return {**recorded, **_breach_update(ctx, state, breach, decision)}

        decision = _stall_consilium(ctx, state, decision, recorded)

        if decision.decision == "consilium" and state.get(
            "consiliums", 0
        ) >= ctx.recipe.limits.max_consiliums * ctx.budget(state):
            decision = JudgeDecision(
                decision="retry",
                reason="consilium budget exhausted; downgraded to retry",
                next_instructions=decision.next_instructions
                or "Consilium budget is spent; fix the most likely cause directly.",
            )

        if decision.decision == "consilium":
            return {
                "stage": "guard",
                "decision": decision.model_dump(),
                "retries_on_tier": 0,
                **recorded,
            }

        retries = state.get("retries_on_tier", 0) + 1
        update: dict[str, Any] = {
            **recorded,
            "stage": "guard",
            "instructions": decision.next_instructions,
            "decision": decision.model_dump(),
            "retries_on_tier": retries,
            # A red check found during the final broader pass re-enters the
            # ordinary retry path right here; resetting the flag means the
            # next `done` gets its own fresh final pass, not a stale one.
            "final_pass": False,
        }
        if state.get("final_pass"):
            update["journal"] = [
                *update.get("journal", []),
                f"iteration {iteration}: the final broader check found a problem -- "
                f"{decision.reason or 'see the last check'}. Fixing it is this task's "
                "own responsibility; back to implement.",
            ]
        if ctx.recipe.complexity.routing == "live" and retries >= ctx.recipe.complexity.escalate_after_retries:
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
                    escalations=[
                        {
                            "from": previous,
                            "to": level,
                            "iteration": iteration,
                            "reason": reason,
                        }
                    ],
                )
        return update

    return guard


def _make_node_route(_dispatch_critics, *, gated: bool = False, verify_node: bool = False):
    """`gated`: the recipe has a `landing_gate`, so a run that would finish
    done goes through the `landing_gate` node (alloy.recipes.landing_gate)
    instead of the final broader pass: the gate already is the operator's
    declared landing step, and a second "broadest check" on top of it ran the
    whole suite twice and timed out against the per-check cap."""

    def route(state: TddState) -> str | list[Send]:
        decision = (state.get("decision") or {}).get("decision", "retry")
        if decision == "done":
            if gated:
                # The landing gate's own checks are the final word; a red one
                # is an ordinary new iteration (see landing_gate.py).
                return "landing_gate"
            # A green iteration goes through one more, broader pass before
            # finishing -- Alloy's replacement for a separate worktree-merge
            # landing step. Only once that pass has itself come back done
            # does the run actually finish.
            if not state.get("final_pass"):
                return "start_final_pass"
            return "finish"
        if decision == "abort":
            return "finish"
        if decision == "human":
            return "human_gate"
        if decision == "verify" and verify_node:
            # guard turned a judge `verify` into checks-only: no new implement round.
            return "verifier_step"
        if decision == "consilium":
            return _dispatch_critics(state)
        return "implement"

    return route


def _make_node_start_final_pass(ctx):
    def start_final_pass(state: TddState) -> dict[str, Any]:
        """Send a green run through the verify loop once more, asking for the
        broadest check available, in place of a separate landing step. Reuses
        the same verifier/run_check/acceptance/judge/guard nodes as any other
        iteration -- a red result there is just an ordinary retry."""
        iteration = state.get("iteration", 0)
        ctx.set_stage("final-pass", iteration=iteration)
        return {
            "stage": "final-pass",
            "final_pass": True,
            "instructions": (
                "Every check has passed. Before this task finishes, propose the "
                "broadest, most complete regression check available for this "
                "change (the full test suite or the closest practical "
                "equivalent), then verify it is green."
            ),
            "journal": [f"iteration {iteration}: starting the final broader check"],
            "verifier_stop": None,
            "pending_check": None,
            "verify_route": None,
        }

    return start_final_pass


def _make_node__dispatch_critics(ctx):
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

    return _dispatch_critics


def _make_node_critic(ctx):
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
            critique = Critique.model_validate({**result.structured, "critic": payload["runner"], "failed": False})
        elif result.ok:
            critique.root_cause = clip(result.text, 1500)
        else:
            critique.root_cause = f"(critic unavailable: {result.error})"
        return {"critiques": [critique.model_dump()]}

    return critic


def _make_node_synthesize(ctx):
    async def synthesize(state: TddState) -> dict[str, Any]:
        ctx.set_stage("synthesize", iteration=state.get("iteration", 0))
        usable = [item for item in state.get("critiques", []) if not item.get("failed")]
        if not usable:
            return {
                "consiliums": state.get("consiliums", 0) + 1,
                "critiques": None,
                "instructions": "Consilium produced no usable opinions; fix the most likely cause directly.",
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

    return synthesize


def _make_node_human_gate(ctx):
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
        instructions = payload if isinstance(payload, str) else (payload or {}).get("instructions", "")
        extra: dict[str, Any] = {}
        if state.get("resume_to") == "tests":
            # The human grants the tests stage a fresh repair budget; without
            # this the very next bad baseline parked the run again at once.
            extra["baseline_repairs"] = 0
            if state.get("baseline"):
                # Parked over the baseline itself: the session that kept
                # proposing it is part of the problem, so start a fresh one.
                extra["tests_session"] = None
        return {
            **extra,
            "instructions": instructions or "Continue; the human provided no extra guidance.",
            "human_note": instructions or "",
            "retries_on_tier": 0,
            "limit_hit": None,
            "limit_breach": None,
            "budget_extensions": state.get("budget_extensions", 0) + 1,
            "stage": "resumed",
            # Where to continue: the stage that parked us (e.g. "tests" after a
            # failed tests role), else the implementer.
            "resume_target": state.get("resume_to") or "implement",
            "resume_to": None,
            "decision": JudgeDecision(
                decision="retry",
                reason="resumed by human",
                next_instructions=instructions,
            ).model_dump(),
        }

    return human_gate


def _make_node_finish(ctx, remember_check_hints, remember_calibration):
    def finish(state: TddState) -> dict[str, Any]:
        ctx.set_stage("finished", iteration=state.get("iteration", 0))
        decision = JudgeDecision.model_validate(state.get("decision") or {"decision": "abort", "reason": "no decision"})
        outcome = Outcome.DONE if decision.decision == "done" else Outcome.FAILED
        if outcome is Outcome.DONE:
            remember_check_hints(state)
        remember_calibration(state)
        return {
            "outcome": outcome.value,
            "outcome_reason": decision.reason,
            "stage": "finished",
        }

    return finish


def _make_node_remember_check_hints(ctx):
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

    return remember_check_hints


def _make_node_remember_calibration(ctx):
    def remember_calibration(state: TddState) -> None:
        """Fold this run into alloy:calibration: per complexity level, how many
        runs finished, their mean iterations and agent calls, and how many hit
        a limit. Every finish counts, DONE or not, so overruns are recorded.
        Nothing is written when memory is off or no bd is bound."""
        if ctx.beads is None or not ctx.recipe.memory.enabled:
            return
        body = update_calibration(
            state.get("memory_calibration", ""),
            level=state.get("complexity") or "medium",
            iterations=state.get("iteration", 0),
            agent_calls=ctx.store.call_count(ctx.run_id),
            overrun=bool(state.get("limit_hit")),
        )
        try:
            ctx.beads.remember(
                CALIBRATION_KEY,
                with_provenance(body, ctx.run_id, ctx.bead.id, utcnow().date()),
            )
        except Exception:
            log.warning("could not remember %s", CALIBRATION_KEY, exc_info=True)

    return remember_calibration


def _make_node_harvest(ctx, remember_lesson):
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
                existing_lessons=state.get("memory_lessons") or {},
            )
        except Exception:
            log.warning("harvest skipped", exc_info=True)
            return {}
        answer = await classify(
            ctx,
            "harvest",
            spec,
            prompt,
            model_cls=HarvestAnswer,
            default=default,
            iteration=state.get("iteration", 0),
        )
        if answer is default:
            log.info("harvest wrote nothing: %s", default.reason)
            return {}
        remember_lesson(state, answer)
        return {}

    return harvest


def _make_node_remember_lesson(ctx):
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
            ctx.beads.remember(key, with_provenance(body, ctx.run_id, ctx.bead.id, utcnow().date()))
            ctx.beads.note(
                ctx.bead.id,
                f"alloy: run {ctx.run_id} harvested repository lesson {key} (confidence {answer.confidence:.2f})",
            )
        except Exception:
            log.warning("could not remember %s", key, exc_info=True)

    return remember_lesson


def _context_from(result) -> ContextPacket:
    if result.structured:
        try:
            return ContextPacket.model_validate(result.structured)
        except Exception:
            pass
    return ContextPacket(
        summary=clip(result.text, 3000) if result.ok else f"(context gathering failed: {result.error})"
    )


def _tests_output_from(result) -> TestsOutput:
    if result.structured:
        try:
            return TestsOutput.model_validate(result.structured)
        except Exception:
            pass
    return TestsOutput(summary=clip(result.text, 3000))
