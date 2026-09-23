"""Point-in-time snapshot of everything Alloy is doing, for `alloy monitor`.

`build_snapshot` is a plain synchronous, read-only function over the data
layer (`Store`, the LangGraph checkpoints, the scheduler pidfile and `bd
ready`). It returns the frozen JSON shape documented under "Component 2" in
docs/plans/execution-monitor.md; the interactive view renders this dict and
`alloy monitor --once --json` prints it verbatim. Every field is always
present: `null` where a value is genuinely absent, never omitted.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from alloy import beads as bd
from alloy.config import ConfigError, RecipeConfig
from alloy.engine import Engine
from alloy.limits import installed_harnesses, read_cache, unavailable
from alloy.runners import RunnerRegistry
from alloy.scheduler import read_pid
from alloy.store import RUN_CANCELLED, RUN_DONE, RUN_FAILED
from alloy.verify import checks_summary

READY_CAP = 1000
LIFETIME_STATUSES = (RUN_DONE, RUN_FAILED, RUN_CANCELLED)


def build_snapshot(engine: Engine) -> dict[str, Any]:
    """One frozen-shape view of the scheduler, the queue and every active run."""
    pid = read_pid(engine.paths.scheduler_pid)
    totals = engine.store.run_status_totals(engine.repo)
    limits = _limits(engine)
    return {
        "root": str(engine.paths.root),
        "repo": str(engine.repo),
        "scheduler": {"running": pid is not None, "pid": pid},
        "ready_count": _ready_count(engine),
        "ready_capped_at": READY_CAP,
        "lifetime": {status: int(totals.get(status, 0)) for status in LIFETIME_STATUSES},
        "limits": limits,
        "runs": [
            _run_entry(engine, record, limits)
            for record in engine.store.active_runs(repo=engine.repo)
        ],
    }


def _limits(engine: Engine) -> dict[str, Any]:
    """Cached limits for installed harnesses only; a cache miss is 'not probed yet'.

    Reads `limits.json` and PATH only -- never probes a harness.
    """
    cached = read_cache(engine.paths)
    return {
        harness: cached.get(harness) or unavailable(harness, "not probed yet")
        for harness in installed_harnesses(RunnerRegistry())
    }


def _ready_count(engine: Engine) -> int:
    try:
        return len(engine.beads.ready(limit=READY_CAP))
    except (bd.BeadsError, OSError):
        return 0  # the queue is unknowable without bd; the runs are still worth showing


def _run_entry(
    engine: Engine, record: dict[str, Any], limits: dict[str, Any]
) -> dict[str, Any]:
    run_id = record["run_id"]
    checkpoint = engine.graph_snapshot_for_run(run_id)
    state: dict[str, Any] = (checkpoint or {}).get("values") or {}
    try:
        config: RecipeConfig | None = engine.load_config(record["recipe"])
    except ConfigError:
        config = None

    # Mirrors RunContext.budget(): every human resume grants one more window.
    budget = 1 + int(state.get("budget_extensions", 0) or 0)
    max_iterations = config.limits.max_iterations * budget if config else 0
    max_consiliums = config.limits.max_consiliums * budget if config else 0

    now = datetime.now(timezone.utc)
    return {
        "bead_id": record["bead_id"],
        "run_id": run_id,
        "parent_run_id": record.get("parent_run_id"),
        "complexity": record.get("complexity"),
        "recipe": record["recipe"],
        "status": record["status"],
        "stage": state.get("stage") or record["stage"],
        "iteration": int(state.get("iteration", record["iteration"]) or 0),
        "max_iterations": max_iterations,
        "consiliums": int(state.get("consiliums", 0) or 0),
        "max_consiliums": max_consiliums,
        "tests_summary": record["tests_summary"],
        "checks": checks_summary(state),
        "elapsed_minutes": _elapsed_minutes(record, now),
        "current_calls": [
            _call_entry(config, call, now) for call in engine.store.active_calls(run_id)
        ],
        "tokens": engine.store.token_totals(run_id),
        "tokens_by_role": engine.store.token_totals_by_role(run_id),
        "judge": _judge(engine, run_id, state),
        "models_used": _models_used(engine, run_id, limits),
        "worktree": record["worktree"],
        "branch": record["branch"],
    }


def _models_used(
    engine: Engine, run_id: str, limits: dict[str, Any]
) -> list[dict[str, Any]]:
    """Store.models_used entries, each joined to its harness's cached windows.

    Account-wide windows (model=None) always attach; a per-model window
    attaches when its family string appears in the run's model, case-insensitively.
    """
    entries = []
    for entry in engine.store.models_used(run_id):
        windows: dict[str, Any] = {}
        sample = limits.get(entry["harness"]) if entry.get("harness") else None
        model = (entry.get("model") or "").lower()
        for cached_window in (sample or {}).get("windows") or []:
            family = cached_window.get("model")
            if family is None or (model and family.lower() in model):
                windows[cached_window["key"]] = cached_window
        entries.append({**entry, "windows": windows})
    return entries


def _call_entry(config: RecipeConfig | None, call: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Requested (recipe) vs effective (ledger) runner/model for one in-flight call."""
    spec = config.roles.get(call["role"]) if config else None
    started = _parse(call["started_at"])
    return {
        "role": call["role"],
        "requested_runner": spec.runner if spec else None,
        "effective_runner": call["runner"],
        "requested_model": spec.model if spec else None,
        "effective_model": call["model"],
        "elapsed_seconds": max(0.0, (now - started).total_seconds()) if started else 0.0,
    }


def _judge(engine: Engine, run_id: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """What the judge said vs what the guard let stand; None before the judge stage."""
    judge_calls = [call for call in engine.store.agent_calls(run_id) if call["role"] == "judge"]
    if not judge_calls:
        return None
    try:
        raw_payload = json.loads(judge_calls[-1]["structured_json"] or "null") or {}
    except ValueError:
        raw_payload = {}
    effective_payload = state.get("decision") or {}
    raw = {"decision": raw_payload.get("decision"), "confidence": raw_payload.get("confidence")}
    effective = {
        "decision": effective_payload.get("decision"),
        "reason": effective_payload.get("reason"),
    }
    return {
        "raw": raw,
        "effective": effective,
        "matches_effective": raw["decision"] == effective["decision"],
    }


def _elapsed_minutes(record: dict[str, Any], now: datetime) -> int:
    """Wall time minus the time spent paused or dead, as `alloy status` counts it."""
    started = _parse(record["started_at"])
    if started is None:
        return 0
    reference = _parse(record.get("ended_at")) or _parse(record.get("paused_at")) or now
    paused_s = float(record.get("paused_s") or 0)
    return max(0, int(((reference - started).total_seconds() - paused_s) // 60))


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
