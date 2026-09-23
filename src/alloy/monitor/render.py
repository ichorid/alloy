"""Pure functions from a monitor snapshot dict to table rows and a header line.

No Textual or Rich involved: `run_rows` and `header_line` take the frozen
JSON shape from docs/plans/execution-monitor.md and return plain strings, so
they are unit-testable and shared by the live view and `alloy monitor --once`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.cells import cell_len

from alloy.limits import HARNESSES
from alloy.monitor.icons import icon

COLUMNS = ("parent", "bead", "recipe", "status", "stage", "iter", "cons", "tests", "elapsed", "now",
           "tokens", "judge", "complexity")

COMFORTABLE_WIDTH = 80
WIDE_WIDTH = 100

_WIDE_ONLY_COLUMNS = frozenset({"parent", "stage", "cons", "complexity"})
_COMFORTABLE_ONLY_COLUMNS = frozenset({"recipe"})
_COLUMN_TIERS: dict[str, str] = {
    **{name: "wide" for name in _WIDE_ONLY_COLUMNS},
    **{name: "comfortable" for name in _COMFORTABLE_ONLY_COLUMNS},
}

_USAGE_BAR_WIDTH = 16
LIMITS_HARNESS_WIDTH = max(len(name) for name in HARNESSES)
# Account-wide window labels (5h, weekly, cycle, ...); pad so bars line up per column.
LIMITS_WINDOW_LABEL_WIDTH = max(len(label) for label in ("5h", "cycle", "weekly"))
LIMITS_ALIGNED_WINDOW_COUNT = 2
_COLOR_GREEN = "#7ee787"
_COLOR_YELLOW = "#e3b341"
_COLOR_RED = "#f85149"
_COLOR_CYAN = "#56b6c2"
_COLOR_MAGENTA = "#d2a8ff"
_COLOR_DIM = "#8b949e"


def column_tier(name: str) -> str:
    """Return the width tier for a column key: always, comfortable, or wide."""
    return _COLUMN_TIERS.get(name, "always")


def visible_columns(width: int) -> tuple[str, ...]:
    """Column keys shown in DataTable#runs at the given terminal width."""
    if width >= WIDE_WIDTH:
        return COLUMNS
    hidden = set(_WIDE_ONLY_COLUMNS)
    if width < COMFORTABLE_WIDTH:
        hidden |= _COMFORTABLE_ONLY_COLUMNS
    return tuple(name for name in COLUMNS if name not in hidden)


def header_line(snapshot: dict[str, Any], mode: str | None = None) -> str:
    """One-line stats summary: scheduler, ready queue and lifetime totals."""
    if mode is None or mode == "ascii":
        return _header_line_ascii(snapshot)
    if mode == "nerd":
        return _header_line_styled(snapshot, "nerd")
    return _header_line_styled(snapshot, "unicode")


def _header_line_ascii(snapshot: dict[str, Any]) -> str:
    scheduler = snapshot.get("scheduler") or {}
    if scheduler.get("running"):
        sched = f"scheduler running (pid {scheduler.get('pid')})"
    else:
        sched = "scheduler stopped"
    ready = f"ready {snapshot.get('ready_count', 0)} (capped at {snapshot.get('ready_capped_at')})"
    lifetime = snapshot.get("lifetime") or {}
    totals = (f"done {lifetime.get('done', 0)}  failed {lifetime.get('failed', 0)}"
              f"  cancelled {lifetime.get('cancelled', 0)}")
    line = f"{sched}  |  {ready}  |  {totals}"
    if "session_totals" in snapshot:
        session = snapshot["session_totals"] or {}
        line += (f"  |  this session: done {session.get('done', 0)}"
                 f" failed {session.get('failed', 0)} cancelled {session.get('cancelled', 0)}")
    return line


def _header_line_styled(snapshot: dict[str, Any], mode: str) -> str:
    scheduler = snapshot.get("scheduler") or {}
    lifetime = snapshot.get("lifetime") or {}
    arrow = icon("arrow", mode)
    if scheduler.get("running"):
        sched = f" {icon('scheduler', mode)} scheduler pid {scheduler.get('pid')}"
    else:
        sched = f" {icon('scheduler', mode)} scheduler stopped"
    ready = f" {icon('queue', mode)} ready {snapshot.get('ready_count', 0)}"
    totals = (
        f"  {icon('done', mode)} {lifetime.get('done', 0)}"
        f"  {icon('test_fail', mode)} {lifetime.get('failed', 0)}"
        f"  {icon('blocked', mode)} {lifetime.get('cancelled', 0)}"
    )
    line = f"{sched}{arrow}{ready}{arrow}{totals}"
    if "session_totals" in snapshot:
        session = snapshot["session_totals"] or {}
        line += (
            f"   {icon('arrow_thin', mode)} session "
            f"done {session.get('done', 0)} failed {session.get('failed', 0)}"
            f" cancelled {session.get('cancelled', 0)}"
        )
    return line


def title_line(snapshot: dict[str, Any], width: int, mode: str) -> str:
    """Powerline title row: alloy branding, monitor label, repo path."""
    arrow = icon("arrow", mode)
    repo = _display_repo(snapshot.get("repo"))
    left = f" {icon('alloy', mode)} alloy {arrow} monitor {arrow} {icon('folder', mode)} {repo} {arrow}"
    now = datetime.now().astimezone().strftime("%H:%M:%S")
    right = (
        f"{icon('arrow_left', mode)} {icon('refresh', mode)} 1s "
        f"{icon('arrow_left', mode)} {icon('clock', mode)} {now} "
    )
    budget = max(0, width - cell_len(right))
    return _fit_cell_width(left, budget) + right


def _display_repo(repo: Any) -> str:
    if not repo:
        return "-"
    text = str(repo)
    home = Path.home()
    try:
        return "~" + str(Path(text).relative_to(home))
    except ValueError:
        return text


def _fit_cell_width(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text + " " * (width - cell_len(text))
    out = ""
    for ch in text:
        if cell_len(out + ch) > width - 1:
            break
        out += ch
    return out + "\u2026" + " " * max(0, width - cell_len(out + "\u2026"))


def run_rows(snapshot: dict[str, Any]) -> list[tuple[str, ...]]:
    """One tuple per `runs[]` entry, in `COLUMNS` order."""
    return [_row(run) for run in snapshot.get("runs") or []]


def status_color(status: str) -> str:
    """Map a run status label to its DataTable badge color."""
    return {
        "running": _COLOR_CYAN,
        "judge": _COLOR_MAGENTA,
        "blocked": _COLOR_RED,
        "done": _COLOR_GREEN,
        "ready": _COLOR_DIM,
    }.get(status, _COLOR_DIM)


def limits_lines(snapshot: dict[str, Any]) -> list[str]:
    """One line per harness in HARNESSES order; empty when top-level limits is {}."""
    limits = snapshot.get("limits") or {}
    if not limits:
        return []
    return [_limits_line(harness, limits[harness]) for harness in HARNESSES if harness in limits]


def _limits_harness_label(harness: str) -> str:
    """Fixed-width harness column so the first usage bar lines up across agents."""
    return harness.ljust(LIMITS_HARNESS_WIDTH)


def _limits_line(harness: str, sample: dict[str, Any]) -> str:
    label = _limits_harness_label(harness)
    if not sample.get("available"):
        error = _text(sample.get("error"))
        line = f"{label}  [{_COLOR_RED}]unavailable: {error}[/]"
        status = sample.get("status")
        if status:
            line += f" [{status}]"
        return line
    parts = [label]
    windows = sample.get("windows") or []
    for index, win in enumerate(windows):
        parts.append(_limits_window_segment(win, align_bar=index < LIMITS_ALIGNED_WINDOW_COUNT))
    line = "  ".join(parts)
    stale = _stale_as_of(sample.get("as_of"))
    if stale:
        line += stale
    status = sample.get("status")
    if status:
        line += f" [{status}]"
    return line


def _usage_color(percent: int) -> str:
    if percent >= 80:
        return _COLOR_RED
    if percent >= 50:
        return _COLOR_YELLOW
    return _COLOR_GREEN


def _usage_bar(percent: int) -> str:
    filled = max(0, min(_USAGE_BAR_WIDTH, round(percent * _USAGE_BAR_WIDTH / 100)))
    return f"[{'█' * filled}{'░' * (_USAGE_BAR_WIDTH - filled)}]"


def _limits_window_head(label: str, percent: int, *, align_bar: bool) -> str:
    if align_bar:
        return f"{label.ljust(LIMITS_WINDOW_LABEL_WIDTH)} {percent:>3}%"
    return f"{label} {percent}%"


def _limits_window_segment(win: dict[str, Any], *, align_bar: bool = False) -> str:
    percent = int(win["used_percent"])
    color = _usage_color(percent)
    head = _limits_window_head(win["label"], percent, align_bar=align_bar)
    segment = f"[{color}]{head} {_usage_bar(percent)}[/]"
    resets = _resets_hhmm(win.get("resets_at"))
    if resets:
        segment += f" (resets {resets})"
    if win.get("stale"):
        segment += f" [{_COLOR_YELLOW}][stale][/]"
    return segment


def _row(run: dict[str, Any]) -> tuple[str, ...]:
    return (
        _text(run.get("parent_bead_id")),
        _text(run.get("bead_id")),
        _text(run.get("recipe")),
        _text(run.get("status")),
        _text(run.get("stage")),
        f"{_text(run.get('iteration'))}/{_text(run.get('max_iterations'))}",
        f"{_text(run.get('consiliums'))}/{_text(run.get('max_consiliums'))}",
        _tests(run),
        _elapsed(run.get("elapsed_minutes")),
        _now(run.get("current_calls") or []),
        _tokens(run.get("tokens") or {}),
        _judge(run.get("judge")),
        _text(run.get("complexity")),
    )


def _text(value: Any) -> str:
    return "-" if value is None else str(value)


def _tests(run: dict[str, Any]) -> str:
    """`n checks` once the verification loop has run anything, else the ledger's
    tests summary verbatim."""
    checks = run.get("checks")
    if isinstance(checks, dict) and checks.get("total") is not None:
        return f"{checks['total']} checks"
    return _text(run.get("tests_summary"))


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


_METADATA_PREFIXES = ("bead:", "worktree:", "branch:", "logs:")


def format_detail(run: dict[str, Any], width: int, log_dir: str | None = None) -> str:
    """Render detail pane text: two columns at comfortable width, stacked below."""
    lines = detail_lines(run, log_dir)
    if width < COMFORTABLE_WIDTH:
        return "\n".join(lines)
    activity = [line for line in lines if not line.startswith(_METADATA_PREFIXES)]
    metadata = [line for line in lines if line.startswith(_METADATA_PREFIXES)]
    left_width = max((len(line) for line in activity), default=0)
    row_count = max(len(activity), len(metadata))
    rows: list[str] = []
    for index in range(row_count):
        left = activity[index] if index < len(activity) else ""
        right = metadata[index] if index < len(metadata) else ""
        if right:
            rows.append(f"{left:<{left_width}}  {right}" if left_width else right)
        else:
            rows.append(left)
    return "\n".join(rows)


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
    for entry in run.get("models_used") or []:
        lines.append(_models_used_line(entry))
    lines.append(f"bead: {_text(run.get('bead_id'))}")
    lines.append(f"worktree: {_text(run.get('worktree'))}")
    lines.append(f"branch: {_text(run.get('branch'))}")
    if log_dir is not None:
        lines.append(f"logs: {log_dir}")
    return lines


def _runner(runner: Any, model: Any) -> str:
    return f"{_text(runner)}:{model}" if model else _text(runner)


def _models_used_line(entry: dict[str, Any]) -> str:
    runner = entry.get("runner")
    model = entry.get("model")
    label = f"{runner}:{model}" if model else _text(runner)
    parts = [
        f"model {label}",
        f"calls {entry.get('calls', 0)}",
        f"tokens {_text(entry.get('total_tokens'))}",
    ]
    window_parts = [
        f"{win['label']} {int(win['used_percent'])}%"
        for win in (entry.get("windows") or {}).values()
    ]
    line = "  ".join(parts)
    if window_parts:
        line += "  |  " + "  ".join(window_parts)
    return line


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _resets_hhmm(resets_at: str | None) -> str | None:
    parsed = _parse_iso(resets_at)
    return parsed.strftime("%H:%M") if parsed else None


def _stale_as_of(as_of: str | None) -> str:
    parsed = _parse_iso(as_of)
    if parsed is None:
        return ""
    age = (datetime.now(timezone.utc) - parsed).total_seconds()
    if age <= 30 * 60:
        return ""
    return f" (as of {parsed.strftime('%H:%M')})"


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
