"""Pure functions from a monitor snapshot dict to table rows and a header line.

No Textual or Rich involved: `run_rows` and `header_line` take the frozen
JSON shape from docs/plans/execution-monitor.md and return plain strings, so
they are unit-testable and shared by the live view and `alloy monitor --once`.
"""

from __future__ import annotations

from typing import Any

COLUMNS = ("bead", "recipe", "status", "stage", "iter", "cons", "tests", "elapsed", "now",
           "tokens", "judge", "complexity")


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
        ("  └ " if run.get("parent_run_id") else "") + _text(run.get("bead_id")),
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
        _text(run.get("complexity")),
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


def detail_lines(run: dict[str, Any], log_dir: str | None = None) -> list[str]:
    """Selected-run detail pane: in-flight calls, raw vs effective judge, per-role tokens, paths.

    `log_dir` is not part of the run dict (it is `{root}/logs/{run_id}`), so the
    caller passes it in; it is omitted when None.
    """
    lines: list[str] = []
    for call in run.get("current_calls") or []:
        requested = _runner(call.get("requested_runner"), call.get("requested_model"))
        effective = _runner(call.get("effective_runner"), call.get("effective_model"))
        seconds = call.get("elapsed_seconds")
        elapsed = "-" if seconds is None else f"{int(seconds)}s"
        lines.append(f"{_text(call.get('role'))}: {requested} -> {effective} ({elapsed})")
    judge_line = _judge_detail(run.get("judge"))
    if judge_line is not None:
        lines.append(judge_line)
    for role, tokens in (run.get("tokens_by_role") or {}).items():
        lines.append(f"{role}: {_text(tokens.get('total_tokens'))} "
                     f"({_text(tokens.get('input_tokens'))}/{_text(tokens.get('output_tokens'))})")
    lines.append(f"worktree: {_text(run.get('worktree'))}")
    lines.append(f"branch: {_text(run.get('branch'))}")
    if log_dir is not None:
        lines.append(f"logs: {log_dir}")
    return lines


def _runner(runner: Any, model: Any) -> str:
    return f"{_text(runner)}:{model}" if model else _text(runner)


def _judge_detail(judge: dict[str, Any] | None) -> str | None:
    if not judge:
        return None
    raw = judge.get("raw") or {}
    effective = judge.get("effective") or {}
    if raw.get("decision") is None:
        return "judge: no parseable verdict" if effective.get("decision") is not None else None
    if raw.get("decision") == effective.get("decision"):
        return f"judge: {raw['decision']} (confidence {_text(raw.get('confidence'))})"
    return (f"judge said {raw['decision']} (confidence {_text(raw.get('confidence'))})"
            f" / Alloy did {_text(effective.get('decision'))} -- {_text(effective.get('reason'))}")
