"""Node behavior for the TDD workflow graph."""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.types import Send, interrupt

from alloy.beads import LABEL_BUG, LABEL_HUMAN, META_DISCOVERED_IN_RUN, META_RECIPE
from alloy.config import RoleSpec
from alloy.models import (
    CALIBRATION_KEY,
    CHECK_HINTS_KEY,
    CONTRADICTION_KEY_PREFIX,
    REGRESSION_KEY_PREFIX,
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
from alloy.recipes.role_prompts import (
    CONTEXT_SCHEMA,
    context_prompt,
    critic_prompt,
    estimate_prompt,
    extract_summary,
    harvest_prompt,
    implement_prompt,
    log,
    synthesize_prompt,
    tests_prompt,
    tests_review_prompt,
)
from alloy.recipes.shared_verification import (
    _check_hints,
    _checks_headline,
    _checks_of,
    _evidence_packet,
    _implementer_changed_tests,
    _is_red,
    _last_check_summary,
    _retry_at_iso,
    _unavailable_decision,
    call_in_session,
    classify,
    resumable_session,
)
from alloy.recipes.state import CriticInput, TddState
from alloy.worktree import is_test_path


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
        spec = ctx.role_spec("tests", state)
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
            update["tests_session"] = {
                "runner": result.runner,
                "session_id": result.session_id,
            }
        return update

    return write_tests


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
            if not result.runnable:
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
        role_spec = ctx.recipe.role("tests_review")
        prompt = tests_review_prompt(
            ctx.bead.task_brief(),
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
            "implement_unavailable": False,
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
        metadata = {key: ctx.bead.metadata[key] for key in (META_RECIPE,) if ctx.bead.metadata.get(key)}
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

    return _file_bug


def _make_node_triage(ctx, _first_available, _park, _file_bug):
    async def triage(state: TddState) -> dict[str, Any]:
        return await triage_reports(state, ctx, _first_available, _park, _file_bug)

    return triage


def _make_node_route_after_triage():
    def route_after_triage(state: TddState) -> str:
        return state.get("triage_route") or "verifier_step"

    return route_after_triage


def _make_node_remediate(ctx):
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
            "bead_id": bug_id,
            "run_id": run_id,
            "outcome": outcome,
            "reason": reason,
            "started_at": started_at,
            "ended_at": utcnow().isoformat(),
        }
        update: dict[str, Any] = {
            "stage": "remediate",
            "remediations": [entry],
            "blocking_bug": None,
        }
        if outcome == Outcome.DONE.value:
            notes = [state["instructions"]] if state.get("instructions") else []
            notes.append(
                f"Bug '{bug.get('title')}' was fixed and merged into this worktree by "
                f"{bug_id}; do not undo it; continue with the task."
            )
            return {**update, "instructions": "\n".join(notes)}
        if ctx.beads is not None and ctx.recipe.memory.enabled:
            try:
                update["memory_regressions"] = {
                    key: body for key, body in ctx.beads.memories().items() if key.startswith(REGRESSION_KEY_PREFIX)
                }
            except Exception:
                log.warning("could not refresh regression memories", exc_info=True)
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

    return remediate


def _make_node_route_after_remediate():
    def route_after_remediate(state: TddState) -> str:
        remediations = state.get("remediations") or []
        last = remediations[-1] if remediations else {}
        return "implement" if last.get("outcome") == Outcome.DONE.value else "human_gate"

    return route_after_remediate


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
        breach = ctx.check_limits(state)
        # Green means: every required check of this iteration passed.
        tests_green = not any(_is_red(check) for check in _checks_of(state, state.get("iteration", 0)))

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

        if breach:
            return {
                **recorded,
                "stage": "guard",
                "limit_hit": breach,
                "decision": JudgeDecision(
                    decision="human",
                    reason=f"Alloy stopped the loop: {breach}. Last judge reason: {decision.reason}",
                    next_instructions=decision.next_instructions,
                    confidence=decision.confidence,
                ).model_dump(),
            }

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
        }
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


def _make_node_route(_dispatch_critics):
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

    return route


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
