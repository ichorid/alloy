"""Status and monitor display helpers."""

from __future__ import annotations

from typing import Any

from alloy import beads as bd
from alloy.config import (
    ConfigError,
    RecipeConfig,
)
from alloy.engine import Engine
from alloy.verify import checks_summary


def _role_label(info: dict[str, Any]) -> str:
    mark = "" if info["available"] else " [yellow](runner missing)[/yellow]"
    model = f":{info['model']}" if info.get("model") else ""
    effort = f"@{info['effort']}" if info.get("effort") else ""
    label = f"{info['runner']}{model}{effort}{mark}"
    if info.get("fallback"):
        label += f"  [dim]-> fallback[/dim] {_role_label(info['fallback'])}"
    return label


def _current_agent(engine: Engine, config: RecipeConfig | None, record: dict[str, Any]) -> dict[str, Any]:
    """The role/runner/model actually behind this run right now.

    `record["stage"]` names the *current* graph node (set by `ctx.set_stage`
    as each node starts), which can be well ahead of the *last completed*
    agent call in `agent_calls` -- e.g. stage "implement" while the most
    recently finished call was still "tests", because the implement call
    itself hasn't returned yet. Showing the last completed call there would
    claim "tests" is still going on when it manifestly isn't (the stage
    already says otherwise), so: when the stage names one of the roles this
    recipe configures directly, trust the stage and show *that* role's
    configured runner/model (resolving the `astra`-style alias to what will
    actually execute) -- it is either running right now or about to be. Only
    fall back to the last completed call for stages with no directly agent
    (`verify`/`baseline` are deterministic Python, not an agent call) or once
    the run has moved somewhere this function doesn't special-case.
    """
    from alloy.runners import ALIASES
    from alloy.store import TERMINAL_RUN_STATUSES

    stage = record.get("stage")
    if record.get("status") in TERMINAL_RUN_STATUSES or record.get("status") == "waiting-human":
        return {"role": None, "runner": None, "model": None}  # nothing is running

    if config and stage in _STAGE_ROLES:
        spec = config.roles.get(stage)
        if spec:
            try:
                # Live routing dispatches tiered roles through the tier chain
                # for the run's complexity (recorded on the run row by estimate).
                spec = config.resolve_role(stage, record.get("complexity"))
            except ConfigError:
                pass  # complexity not known yet: show the role's own spec
            # A failed primary call for this very stage/iteration means the
            # fallback is what is running now.
            calls = engine.store.agent_calls(record["run_id"])
            last = calls[-1] if calls else None
            while (
                spec.fallback is not None
                and last is not None
                and last["role"] == stage
                and not last["ok"]
                and last["iteration"] == record.get("iteration", 0)
                and ALIASES.get(spec.runner, spec.runner) == ALIASES.get(last["runner"], last["runner"])
            ):
                spec = spec.fallback
                calls = [c for c in calls if c is not last]
                last = calls[-1] if calls else None
            runner = ALIASES.get(spec.runner, spec.runner)
            return {"role": stage, "runner": runner, "model": spec.model}
    if config and stage == "consilium":
        return {"role": stage, "runner": "multiple critics", "model": None}
    if config and stage == "synthesize":
        spec = config.consilium.synthesizer
        runner = ALIASES.get(spec.runner, spec.runner)
        return {"role": stage, "runner": runner, "model": spec.model}

    calls = engine.store.agent_calls(record["run_id"])
    if calls:
        last = calls[-1]
        return {"role": last["role"], "runner": last["runner"], "model": last["model"]}
    return {"role": stage, "runner": None, "model": None}


def _status_row(engine: Engine, record: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime, timezone

    snapshot = engine.graph_snapshot_for_run(record["run_id"])
    state = (snapshot or {}).get("values") or {}
    try:
        config = engine.load_config(record["recipe"])
        max_iterations = config.limits.max_iterations
    except ConfigError:
        config, max_iterations = None, 0

    agent = _current_agent(engine, config, record)

    started = _parse(record["started_at"])
    ended = _parse(record["ended_at"]) if record["ended_at"] else None
    paused_at = _parse(record.get("paused_at")) if record.get("paused_at") else None
    reference = ended or paused_at or datetime.now(timezone.utc)
    paused_s = float(record.get("paused_s") or 0)  # time parked or dead: not work
    elapsed = max(0, int(((reference - started).total_seconds() - paused_s) // 60)) if started else 0

    parent_run_id = record.get("parent_run_id")
    parent_bead_id = None
    if parent_run_id:
        parent = engine.store.get_run(parent_run_id)
        if parent:
            parent_bead_id = parent["bead_id"]

    return {
        "bead": record["bead_id"],
        "run_id": record["run_id"],
        "parent_run_id": parent_run_id,
        "parent_bead_id": parent_bead_id,
        "children": [child["run_id"] for child in engine.store.children_of(record["run_id"])],
        "remediating": (record["stage"].split(":", 1)[1] if (record["stage"] or "").startswith("remediate:") else None),
        "recipe": record["recipe"],
        "status": record["status"],
        "stage": record["stage"],
        "iteration": record["iteration"],
        "max_iterations": max_iterations,
        "consiliums": record["consiliums"],
        "agent_calls": record["agent_calls"],
        "tests": record["tests_summary"],
        "checks": checks_summary(state),
        "elapsed": f"{elapsed}m",
        "elapsed_minutes": elapsed,
        "agent_role": agent["role"],
        "runner": agent["runner"],
        "model": agent["model"],
        "complexity": record.get("complexity"),
        "complexity_source": state.get("complexity_source"),
        "dispatch_tier": record.get("dispatch_tier"),
        "worktree": record["worktree"],
        "branch": record["branch"],
        "log_dir": record["log_dir"],
        "outcome": record["outcome"],
        "outcome_reason": record["outcome_reason"],
    }


def _bead_row(engine: Engine, bead: bd.Bead, ready_ids: set[str]) -> dict[str, Any]:
    """One bead, merged with its most recent run (if it has ever been run)."""
    row: dict[str, Any] = {
        "bead": bead.id,
        "title": bead.title,
        "priority": bead.priority,
        "bead_status": bead.status,
        "ready": bead.id in ready_ids,
        "recipe": bead.recipe,
        "landing": _landing_of(bead),
    }
    record = engine.store.latest_run_for_bead(bead.id)
    if record:
        run_row = _status_row(engine, record)
        run_row.pop("bead", None)
        run_row.pop("recipe", None)
        row.update(run_row)
    else:
        row.update(_EMPTY_RUN_FIELDS)
    return row


def _status_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    if row["ready"]:
        rank = 2
    else:
        rank = _STATUS_RANK.get(row["bead_status"], 2)
    return (rank, row["priority"], row["bead"])


def _agent_label(row: dict[str, Any]) -> str:
    if not row.get("runner"):
        return "-"
    model = f":{row['model']}" if row.get("model") else ""
    return f"{row.get('agent_role') or '?'}:{row['runner']}{model}"


def _truncate(text: str | None, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"


def _parse(value: str | None):
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _coloured(status: str) -> str:
    colour = {
        "running": "cyan",
        "done": "green",
        "waiting-human": "yellow",
        "failed": "red",
        "cancelled": "dim",
    }.get(status, "white")
    return f"[{colour}]{status}[/{colour}]"


def _landing_of(bead: bd.Bead) -> dict[str, Any]:
    """The bead's landing metadata as one status object."""
    return {
        "state": bead.metadata.get(bd.META_LAND_STATE),
        "sha": bead.metadata.get(bd.META_LAND_SHA),
        "repair": bead.metadata.get(bd.META_LAND_REPAIR),
    }


_STAGE_ROLES = ("context", "tests", "implement", "judge")

_EMPTY_RUN_FIELDS: dict[str, Any] = {
    "parent_run_id": None,
    "parent_bead_id": None,
    "children": [],
    "remediating": None,
    "complexity": None,
    "complexity_source": None,
    "dispatch_tier": None,
    "run_id": None,
    "status": None,
    "stage": None,
    "iteration": 0,
    "max_iterations": None,
    "consiliums": 0,
    "agent_calls": 0,
    "tests": None,
    "checks": None,
    "elapsed": "-",
    "elapsed_minutes": 0,
    "agent_role": None,
    "runner": None,
    "model": None,
    "worktree": None,
    "branch": None,
    "log_dir": None,
    "outcome": None,
    "outcome_reason": None,
}

_STATUS_RANK = {
    bd.STATUS_IMPLEMENTING: 0,
    bd.STATUS_WAITING_HUMAN: 1,
    bd.STATUS_REVIEW_READY: 3,
    bd.STATUS_FAILED: 4,
    bd.STATUS_DONE: 5,
}
