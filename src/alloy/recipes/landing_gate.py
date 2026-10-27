"""The landing gate: the last word before a bead finishes done.

A recipe's `landing_gate:` maps path globs to checks:

    landing_gate:
      - paths: ["packages/client/**"]
        checks: [{cmd: "./scripts/run_tests.sh client-gates", purpose: "client gates", timeout_minutes: 15}]
      - paths: ["packages/server/**"]
        checks: [{cmd: "./scripts/run_tests.sh server", purpose: "server suite", timeout_minutes: 30}]

When guard would finish a run as done (a final pass that came back done, or a
budget-landed stop), the graph goes through `landing_gate` instead: it takes
the paths the bead's diff touches relative to the run's base, selects every
entry whose globs match, and runs those checks on the final tree. Only the
run's own check cache can answer one without running it, and that cache is
keyed on the unchanged tree, so a pass from an earlier tree never counts.

All green: the run finishes. A red check sends the run back to `implement`
with the failure as its instructions -- a new iteration -- or, when a run
limit is already spent (a budget-landed stop included), parks it at the human
gate. Without `landing_gate` the graph has no such node and runs as before.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Iterable

from alloy.config import CommandSpec, LandingGateEntry
from alloy.models import CheckRequest, CheckResult, JudgeDecision, clip
from alloy.recipes.role_prompts import log

GATE_OUTPUT_CHARS = 2000
LANDING_GATE_PURPOSE = "landing gate"


@lru_cache(maxsize=256)
def _glob_regex(glob: str) -> re.Pattern[str]:
    """`**` spans any number of directories (none included), `*` and `?`
    stay within one path segment."""
    pattern = ""
    index = 0
    while index < len(glob):
        if glob.startswith("**/", index):
            pattern += "(?:.*/)?"
            index += 3
        elif glob.startswith("/**", index) and index + 3 == len(glob):
            pattern += "(?:/.*)?"
            index += 3
        elif glob.startswith("**", index):
            pattern += ".*"
            index += 2
        elif glob[index] == "*":
            pattern += "[^/]*"
            index += 1
        elif glob[index] == "?":
            pattern += "[^/]"
            index += 1
        else:
            pattern += re.escape(glob[index])
            index += 1
    return re.compile(pattern)


def path_matches(path: str, glob: str) -> bool:
    return _glob_regex(glob.strip().lstrip("/")).fullmatch(path.strip().lstrip("/")) is not None


def gate_checks(entries: Iterable[LandingGateEntry], changed_paths: Iterable[str]) -> list[CommandSpec]:
    """Every check of every entry a changed path matches, in recipe order,
    each command once."""
    paths = list(changed_paths)
    selected: dict[str, CommandSpec] = {}
    for entry in entries:
        if any(path_matches(path, glob) for path in paths for glob in entry.paths):
            for spec in entry.checks:
                selected.setdefault(spec.cmd, spec)
    return list(selected.values())


def _label(spec: CommandSpec) -> str:
    return f"`{spec.cmd}` ({spec.purpose})" if spec.purpose else f"`{spec.cmd}`"


def _make_node_landing_gate(ctx):
    async def landing_gate(state: dict[str, Any]) -> dict[str, Any]:
        iteration = state.get("iteration", 0)
        ctx.set_stage("landing-gate", iteration=iteration)
        selected = gate_checks(ctx.recipe.landing_gate, ctx.worktrees.changed_files(ctx.worktree, keep_lockfiles=True))
        records: list[dict[str, Any]] = []
        for spec in selected:
            request = CheckRequest(command=spec.cmd, purpose=spec.purpose or LANDING_GATE_PURPOSE, kind="regression")
            timeout_s = spec.timeout_minutes * 60 if spec.timeout_minutes is not None else None
            result = await ctx.run_check(request, timeout_s=timeout_s)
            result.iteration = iteration
            records.append(result.model_dump(mode="json"))
            if not result.ok:
                return {"checks": records, **_gate_failed(ctx, state, spec, result)}
        if records:
            log.info("%s: landing gate green (%d check(s))", ctx.bead.id, len(records))
        return {"checks": records, "stage": "landing-gate", "landing_gate_route": "finish"}

    return landing_gate


def _gate_failed(ctx, state: dict[str, Any], spec: CommandSpec, result: CheckResult) -> dict[str, Any]:
    iteration = state.get("iteration", 0)
    summary = (
        f"landing gate check {_label(spec)} failed on the final tree "
        f"({result.headline()}; log: {result.log_path or '(no log)'})"
    )
    instructions = (
        f"The {summary}.\n{clip(result.output_tail, GATE_OUTPUT_CHARS)}\n\n"
        "This bead's diff touches the paths that check guards. Fix the cause without weakening, "
        "skipping or narrowing the check; the landing gate runs again before the bead can finish."
    )
    update: dict[str, Any] = {
        "stage": "landing-gate",
        "last_check": result.model_dump(mode="json"),
        "final_pass": False,
        "verify_route": None,
        "journal": [f"iteration {iteration}: the {summary}; fixing it is this task's own responsibility"],
    }
    if state.get("budget_landed") is not None:
        update["budget_landed"] = None
    breach = ctx.check_limits(state, include_iterations=True)
    if breach is None and state.get("budget_landed"):
        breach = state.get("limit_hit") or "a run budget"
    if breach:
        log.warning("%s: %s; %s -- parking", ctx.bead.id, summary, breach)
        return {
            **update,
            "landing_gate_route": "human_gate",
            "limit_hit": breach,
            "resume_to": "implement",
            "decision": JudgeDecision(
                decision="human",
                reason=f"{summary}; Alloy stopped the loop: {breach}",
                next_instructions=instructions,
            ).model_dump(),
        }
    log.warning("%s: %s; back to implement", ctx.bead.id, summary)
    return {
        **update,
        "landing_gate_route": "implement",
        "instructions": instructions,
        "decision": JudgeDecision(decision="retry", reason=summary, next_instructions=instructions).model_dump(),
    }


def route_after_landing_gate(state: dict[str, Any]) -> str:
    return state.get("landing_gate_route") or "finish"
