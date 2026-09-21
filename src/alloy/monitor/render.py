"""Pure functions from a monitor snapshot dict to table rows and a header line.

No Textual or Rich involved: `run_rows` and `header_line` take the frozen
JSON shape from docs/plans/execution-monitor.md and return plain strings, so
they are unit-testable and shared by the live view and `alloy monitor --once`.
"""

from __future__ import annotations

from typing import Any

COLUMNS = ("bead", "recipe", "status", "stage", "iter", "cons", "tests", "elapsed", "now",
           "tokens", "judge")


def header_line(snapshot: dict[str, Any]) -> str:
    """One-line stats summary: scheduler, ready queue and lifetime totals."""
    scheduler = snapshot.get("scheduler") or {}
    if scheduler.get("running"):
        sched = f"scheduler running (pid {scheduler.get('pid')})"
    else:
        sched = "scheduler stopped"
    ready = f"ready {snapshot.get('ready_count', 0)} (capped at {snapshot.get('ready_capped_at')})"
    lifetime = snapshot.get("lifetime") or {}
    totals = (f"done {lifetime.get('done', 0)}  failed {lifetime.get('failed', 0)}"
              f"  cancelled {lifetime.get('cancelled', 0)}")
    return f"{sched}  |  {ready}  |  {totals}"


def run_rows(snapshot: dict[str, Any]) -> list[tuple[str, ...]]:
    """One tuple per `runs[]` entry, in `COLUMNS` order."""
    return [_row(run) for run in snapshot.get("runs") or []]


def _row(run: dict[str, Any]) -> tuple[str, ...]:
    return (
        _text(run.get("bead_id")),
        _text(run.get("recipe")),
        _text(run.get("status")),
        _text(run.get("stage")),
        f"{_text(run.get('iteration'))}/{_text(run.get('max_iterations'))}",
        f"{_text(run.get('consiliums'))}/{_text(run.get('max_consiliums'))}",
        _text(run.get("tests_summary")),
        _elapsed(run.get("elapsed_minutes")),
        _now(run.get("current_calls") or []),
        _tokens(run.get("tokens") or {}),
        _judge(run.get("judge")),
    )


def _text(value: Any) -> str:
    return "-" if value is None else str(value)


def _elapsed(minutes: Any) -> str:
    return "-" if minutes is None else f"{minutes}m"


def _now(calls: list[dict[str, Any]]) -> str:
    if not calls:
        return "-"
    return " + ".join(_call(call) for call in calls)


def _call(call: dict[str, Any]) -> str:
    label = f"{_text(call.get('role'))}:{_text(call.get('effective_runner'))}"
    model = call.get("effective_model")
    if model:
        label += f":{model}"
    seconds = call.get("elapsed_seconds")
    return label if seconds is None else f"{label} {int(seconds)}s"


def _tokens(tokens: dict[str, Any]) -> str:
    inp, out = tokens.get("input_tokens"), tokens.get("output_tokens")
    if inp is not None and out is not None:
        return f"{inp}/{out}"
    return _text(tokens.get("total_tokens"))


def _judge(judge: dict[str, Any] | None) -> str:
    if not judge:
        return "-"
    raw = (judge.get("raw") or {}).get("decision")
    effective = (judge.get("effective") or {}).get("decision")
    if raw is not None and effective is not None and raw != effective:
        return f"{raw}→{effective}"
    return _text(effective if effective is not None else raw)
