"""Bug triage, scope decisions, and remediation helpers."""

from __future__ import annotations

from typing import Any

from alloy.models import (
    BugReport,
    BugTriage,
    ScopeVerdict,
    _load_calibration,
)
from alloy.recipes.role_prompts import REMEDIATION_MIN_AGENT_CALLS, scope_prompt, triage_prompt
from alloy.recipes.shared_verification import _checks_of, classify, limit_gate, limit_stop
from alloy.recipes.state import TddState
from alloy.runtime import RunContext


class TriageFailure:
    """`classify` default for the triage role: never a severity, so the run
    can only go to the human gate when triage did not answer."""

    severity = None

    def __init__(self) -> None:
        self.reason = ""


def untriaged(state: TddState) -> list[dict[str, Any]]:
    done = set(state.get("triaged_titles") or [])
    return [bug for bug in state.get("reported_bugs", []) if bug["title"] not in done]


def remediation_call_estimate(state: TddState) -> float:
    """Estimated agent calls a remediation child will spend: the stored
    alloy:calibration mean for this run's complexity level, floored at the
    recipe minimum so an empty or unreadable calibration still gates
    deterministically."""
    data = _load_calibration(state.get("memory_calibration", ""))
    entry = data.get(state.get("complexity") or "medium") or {}
    mean = float(entry.get("mean_agent_calls", 0.0) or 0.0)
    return max(mean, float(REMEDIATION_MIN_AGENT_CALLS))


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
    from alloy.recipes.tdd_loop import classify

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
    reason = f"{verdict.verdict}: {verdict.reason}"
    changed = _changed_files(diff)
    needed = [path for path in verdict.needed_files if path in changed] or verdict.needed_files
    incidental = [path for path in changed if path not in needed]
    if needed:
        reason += f". Needed for the fix: {', '.join(needed)}"
        if incidental:
            reason += f"; not needed: {', '.join(incidental)}"
    elif changed:
        reason += f". Changed files: {', '.join(changed)}"
    return False, reason


def _changed_files(diff: str) -> list[str]:
    """Paths a unified diff touches, in diff order (the b/ side)."""
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = line.rsplit(" b/", 1)[-1]
            if path not in files:
                files.append(path)
    return files


def bug_acceptance(report: BugReport) -> str:
    return (
        f"The defect no longer reproduces: {report.evidence or report.title}. "
        "The existing test suite stays green. Production changes are limited to where "
        "the defect lives. Test-only edits in other files are allowed when they only "
        "stabilize the suite against ambient environment or routing (for example "
        "monkeypatching env vars the test should control, or forcing shadow routing in "
        "harness tests under live complexity routing) and do not weaken assertions."
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


def _blocking_triage_result(ctx, state, report, bug_id, entry, verdict, update, notes, _park):
    """A blocking finding is this same task's own responsibility: fold it into
    `instructions` and the run's `journal`, then go straight back to
    `implement` -- no separate bead run, no worktree, no merge. Whatever the
    final broader check turns up later is folded in the same way."""
    notes.append(
        f"Blocking issue found (filed as {bug_id} for the record): {report.title} -- "
        f"{verdict.reason}. Fixing it is part of this task; continue and make it green."
    )
    return {
        **update,
        "triage_route": "implement",
        "instructions": "\n".join(notes),
        "journal": [f"iteration {state.get('iteration', 0)}: blocking issue folded in -- {report.title}"],
    }


async def triage_reports(state: TddState, ctx, _first_available, _park, _file_bug) -> dict[str, Any]:
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
        "stage": "triage",
        "triaged_titles": triaged,
        "filed_bugs": filed,
    }
    def stopped(breach: str) -> dict[str, Any]:
        # The reports left untriaged are triaged after the human resumes
        # (implement -> triage); what was already decided stays in `notes`.
        return {
            **update,
            "triage_route": "guard",
            "instructions": "\n".join(notes),
            **limit_stop(breach, f"triage in iteration {iteration}", next_instructions="\n".join(notes)),
        }

    for raw in untriaged(state):
        report = BugReport.model_validate(raw)
        where = f"{report.where or '?'}: {report.evidence}"
        breach = limit_gate(ctx, state, include_iterations=False)
        if breach:
            return stopped(breach)
        spec = _first_available(configured)
        if spec is None:
            return {
                **update,
                **_park(
                    f"no triage runner is available ({configured.label}) for bug report "
                    f"'{report.title}' at {where}; nothing was filed.",
                    "Install or configure the triage runner, then resume; the report "
                    "is triaged before the implementer runs again.",
                ),
            }
        failure = TriageFailure()
        verdict = await classify(
            ctx,
            "triage",
            spec,
            triage_prompt(
                ctx.bead.task_brief(),
                ctx.bead.acceptance_criteria,
                _checks_of(state, iteration),
                raw,
                filed,
                remediations,
                project_context(state) if callable(project_context) else None,
            ),
            model_cls=BugTriage,
            default=failure,
            iteration=iteration,
        )
        if verdict is failure:
            breach = limit_gate(ctx, state, include_iterations=False)
            if breach:
                return stopped(breach)
            return {
                **update,
                **_park(
                    f"{failure.reason} while triaging bug report '{report.title}' at {where}; nothing was filed.",
                    "Decide what to do with the report, then resume.",
                ),
            }
        triaged.append(report.title)
        severity = verdict.severity
        if severity == "not-a-bug":
            notes.append(
                f"Triage rejected your report '{report.title}': it is part of this task. "
                "Continue and make the tests pass."
            )
            continue
        if severity == "duplicate":
            notes.append(f"Bug '{report.title}' duplicates a report already filed in this run; proceed with the task.")
            continue
        try:
            bug_id = _file_bug(report, verdict)
        except Exception as exc:
            return {
                **update,
                **_park(
                    f"could not file bug '{report.title}' ({severity}) at {where}: {exc}",
                    "File or dismiss the bug by hand, then resume.",
                ),
            }
        entry = {
            "bead_id": bug_id,
            "title": report.title,
            "where": report.where,
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
            return _blocking_triage_result(ctx, state, report, bug_id, entry, verdict, update, notes, _park)
        return {
            **update,
            **_park(
                f"bug '{report.title}' ({bug_id}) needs a human: {verdict.reason}. "
                f"Bead {ctx.bead.id} is now blocked by {bug_id}.",
                f"Resolve {bug_id} (see `bd human list`), then resume; the implementer "
                "runs again with your instructions.",
            ),
        }
    return {
        **update,
        "triage_route": "implement" if state.get("implementer_stopped") else "verifier_step",
        "instructions": "\n".join(notes),
    }
