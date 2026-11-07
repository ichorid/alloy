"""`alloy metrics`: how well the factory is doing, from the run ledger, the
attention feed and the bugs Alloy filed.

`compute_metrics` is a pure function over rows, so the report can be tested
and reused (the bench in `bench/` reads its JSON); `metrics_impl` only loads
the rows and prints. Check durations are not reported: the ledger keeps a
check only in the run's graph state (and the check cache only passes).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from rich.table import Table

from alloy.cli_common import _emit, _engine, _fail, console
from alloy.events import (
    EVENT_FLAKY_CHECK,
    EVENT_NEEDS_HUMAN,
    EVENT_STALLED,
    parse_since,
)
from alloy.usage import normalize

_LIMIT = re.compile(r"\b(max_[a-z_]+) reached")
_EXHAUSTION = re.compile(r"^Alloy stopped the loop \(([a-z-]+)\):")
_ID = re.compile(r"\b[a-z][a-z0-9]*-[a-z0-9]{2,}(?:\.\d+)*\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# (substring, cause) in the order they are tried; the first hit wins.
# (prefix, cause): a reason that starts with the prefix (case-insensitive).
_PREFIX_CAUSES: tuple[tuple[str, str], ...] = (
    ("baseline not runnable", "baseline-not-runnable"),
    ("baseline unexpectedly green", "baseline-green"),
    ("the tests role failed", "tests-role-failed"),
    ("landing gate check", "landing-gate"),
    ("bug '", "bug-needs-human"),
    ("blocking bug", "blocking-bug"),
    ("remediation of blocking bug", "blocking-bug"),
    ("agent call headroom too low", "blocking-bug"),
    ("two unrunnable", "unrunnable-checks"),
    ("the verifier proposed two commands", "unrunnable-checks"),
    ("the judge answered with placeholder text", "judge-glitch"),
)
# (substring, cause) for texts Alloy builds with a variable head.
_CONTAINS_CAUSES: tuple[tuple[str, str], ...] = (
    ("verification budget is spent", "max_total_checks"),
    ("primary and fallback unavailable", "runner-unavailable"),
    ("answered with placeholder text twice", "judge-glitch"),
)


def needs_human_cause(record: dict[str, Any]) -> str:
    """One short cause for a `needs-human` event: the event's own `cause`
    field when it has one, else read from the reason text Alloy wrote. A
    reason Alloy did not template (the judge's own prose) is `judge-human`."""
    cause = record.get("cause")
    if isinstance(cause, str) and cause:
        return cause
    reason = str(record.get("reason") or "")
    limit = _LIMIT.search(reason)
    if limit and reason.startswith("Alloy stopped the loop"):
        return limit.group(1)
    lowered = reason.lower()
    for prefix, name in _PREFIX_CAUSES:
        if lowered.startswith(prefix):
            return name
    for needle, name in _CONTAINS_CAUSES:
        if needle in lowered:
            return name
    if limit:
        return limit.group(1)
    return "judge-human" if reason else "unknown"


def _title_key(title: str) -> str:
    text = _ID.sub("#", title.lower())
    text = _NUMBER.sub("#", text)
    return " ".join(text.split())


def _aware(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return round(sum(items) / len(items), 2) if items else None


def _wall_minutes(run: dict[str, Any]) -> float | None:
    start, end = _aware(run.get("started_at")), _aware(run.get("ended_at"))
    if start is None or end is None:
        return None
    return max(0.0, ((end - start).total_seconds() - float(run.get("paused_s") or 0.0)) / 60)


def compute_metrics(
    runs: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    events: list[dict[str, Any]],
    bugs: list[dict[str, Any]],
    *,
    recipe: str | None = None,
) -> dict[str, Any]:
    """The report. `runs` and `calls` are ledger rows, `events` attention-feed
    records, `bugs` raw `bd list --json` rows of beads Alloy filed. With
    `recipe`, only that recipe's runs (and their calls and events) count."""
    if recipe is not None:
        runs = [run for run in runs if run.get("recipe") == recipe]
    run_ids = {run["run_id"] for run in runs}
    calls = [call for call in calls if call.get("run_id") in run_ids]
    if recipe is not None:
        events = [event for event in events if event.get("run") in run_ids]

    top_runs = [run for run in runs if not run.get("parent_run_id")]
    done_runs = [run for run in top_runs if run.get("status") == "done"]
    runs_per_bead = Counter(run["bead_id"] for run in top_runs)
    walls = [w for w in (_wall_minutes(run) for run in done_runs) if w is not None]

    by_role: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    tokens_total = 0
    cost_total: float | None = None
    for call in calls:
        usage = normalize(json.loads(call.get("usage_json") or "{}"))
        hours = float(call.get("duration_s") or 0.0) / 3600
        failed = 0 if call.get("ok") else 1
        model_key = f"{call.get('runner')}/{call.get('model') or '-'}"
        for table, key in ((by_role, call.get("role") or "?"), (by_model, model_key)):
            entry = table.setdefault(key, {"calls": 0, "failed": 0, "hours": 0.0, "tokens": 0, "cost_usd": None})
            entry["calls"] += 1
            entry["failed"] += failed
            entry["hours"] += hours
            entry["tokens"] += usage["total_tokens"] or 0
            if usage["cost_usd"] is not None:
                entry["cost_usd"] = (entry["cost_usd"] or 0.0) + usage["cost_usd"]
        tokens_total += usage["total_tokens"] or 0
        if usage["cost_usd"] is not None:
            cost_total = (cost_total or 0.0) + usage["cost_usd"]
    for table in (by_role, by_model):
        for entry in table.values():
            entry["hours"] = round(entry["hours"], 2)
            if entry["cost_usd"] is not None:
                entry["cost_usd"] = round(entry["cost_usd"], 2)

    event_counts = Counter(str(event.get("event")) for event in events)
    causes = Counter(needs_human_cause(event) for event in events if event.get("event") == EVENT_NEEDS_HUMAN)
    exhaustion = Counter(
        match.group(1)
        for event in events
        if event.get("event") == EVENT_NEEDS_HUMAN and (match := _EXHAUSTION.match(str(event.get("reason") or "")))
    )
    parks = sum(1 for event in events if event.get("event") == EVENT_STALLED and event.get("stage") == "parked")

    titles = Counter(_title_key(str(bug.get("title") or "")) for bug in bugs)
    duplicates = [{"title": title, "count": n} for title, n in titles.most_common() if n > 1]

    return {
        "recipe": recipe,
        "runs": {
            "total": len(runs),
            "top_level": len(top_runs),
            "remediation": len(runs) - len(top_runs),
            "by_status": dict(Counter(str(run.get("status")) for run in top_runs)),
            "by_outcome": dict(Counter(str(run.get("outcome")) for run in top_runs)),
        },
        "beads": {
            "total": len(runs_per_bead),
            "done": len({run["bead_id"] for run in done_runs}),
            "with_multiple_runs": sum(1 for n in runs_per_bead.values() if n > 1),
        },
        "done_runs": {
            "count": len(done_runs),
            "iterations_mean": _mean(float(run.get("iteration") or 0) for run in done_runs),
            "agent_calls_mean": _mean(float(run.get("agent_calls") or 0) for run in done_runs),
            "wall_minutes_p50": _round(_percentile(walls, 0.5)),
            "wall_minutes_p90": _round(_percentile(walls, 0.9)),
        },
        "agents": {
            "calls": len(calls),
            "failed": sum(entry["failed"] for entry in by_role.values()),
            "hours": round(sum(entry["hours"] for entry in by_role.values()), 2),
            "tokens": tokens_total,
            "cost_usd": round(cost_total, 2) if cost_total is not None else None,
            "by_role": dict(sorted(by_role.items(), key=lambda item: -item[1]["hours"])),
            "by_model": dict(sorted(by_model.items(), key=lambda item: -item[1]["hours"])),
        },
        "events": dict(event_counts),
        "needs_human": {
            "total": sum(causes.values()),
            "by_cause": dict(causes.most_common()),
            # Limit stops by `classify_exhaustion` class (stalled, nearly-done, ...).
            "by_exhaustion": dict(exhaustion.most_common()),
        },
        "parks": parks,
        "flaky_checks": event_counts.get(EVENT_FLAKY_CHECK, 0),
        "bugs": {
            "filed": len(bugs),
            "open": sum(1 for bug in bugs if bug.get("status") not in ("closed", "tombstone")),
            "duplicates": duplicates,
        },
    }


def _round(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def metrics_impl(repo, root, json_out: bool, since: str | None, recipe: str | None) -> None:
    engine = _engine(repo, root)
    try:
        since_at = parse_since(since) if since else None
    except ValueError as exc:
        _fail(str(exc))
        return
    since_iso = since_at.isoformat() if since_at else None
    runs, calls = engine.store.metrics_rows(engine.repo, since_iso)
    events = engine.store.events.read(since=since_at)
    try:
        bugs = engine.beads.filed_bugs()
    except Exception as exc:  # the report must not fail on a bd hiccup
        console.print(f"[yellow]bd: could not list filed bugs: {exc}[/]")
        bugs = []
    if since_at is not None:
        bugs = [bug for bug in bugs if (_aware(bug.get("created_at")) or since_at) >= since_at]
    report = compute_metrics(runs, calls, events, bugs, recipe=recipe)
    report["since"] = since_iso
    if json_out:
        _emit(report, True)
        return
    _print(report)


def _print(report: dict[str, Any]) -> None:
    runs, beads, done = report["runs"], report["beads"], report["done_runs"]
    agents, human, bugs = report["agents"], report["needs_human"], report["bugs"]
    console.print(f"runs {runs['top_level']} (+{runs['remediation']} remediation)  by status {runs['by_status']}")
    console.print(f"beads {beads['total']}  done {beads['done']}  with 2+ runs {beads['with_multiple_runs']}")
    console.print(
        f"done runs: iterations {done['iterations_mean']}  agent calls {done['agent_calls_mean']}  "
        f"wall min p50 {done['wall_minutes_p50']} p90 {done['wall_minutes_p90']}"
    )
    cost = f"${agents['cost_usd']}" if agents["cost_usd"] is not None else "unpriced"
    console.print(
        f"agents: {agents['calls']} calls ({agents['failed']} failed), {agents['hours']} h, "
        f"{agents['tokens']} tokens, {cost} (claude runners only report cost)"
    )
    table = Table("role", "calls", "failed", "hours", "tokens", "cost $")
    for role, entry in agents["by_role"].items():
        table.add_row(
            role,
            str(entry["calls"]),
            str(entry["failed"]),
            f"{entry['hours']:.2f}",
            str(entry["tokens"]),
            "" if entry["cost_usd"] is None else f"{entry['cost_usd']:.2f}",
        )
    console.print(table)
    console.print(f"needs-human {human['total']}: {human['by_cause']}")
    console.print(f"parked {report['parks']}  flaky checks {report['flaky_checks']}  events {report['events']}")
    console.print(f"bugs filed {bugs['filed']} (open {bugs['open']}), duplicate titles {len(bugs['duplicates'])}")
    for dup in bugs["duplicates"][:10]:
        console.print(f"  {dup['count']}x {dup['title'][:100]}")
