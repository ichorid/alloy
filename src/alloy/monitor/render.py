"""Pure functions from a monitor snapshot dict to table rows and a header line.

No Textual or Rich involved: `run_rows` and `header_line` take the frozen
JSON shape from docs/plans/execution-monitor.md and return plain strings, so
they are unit-testable and shared by the live view and `alloy monitor --once`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.cells import cell_len
from rich.text import Text

from alloy.limits import HARNESSES
from alloy.monitor.icons import icon
from alloy.verify import parse_counts

COLUMNS = ("bead", "recipe", "status", "stage", "iter", "cons", "tests", "elapsed", "now",
           "tokens", "judge", "complexity")

RIGHT_ALIGNED = frozenset({"iter", "cons", "tests", "elapsed", "tokens"})

COMFORTABLE_WIDTH = 80
WIDE_WIDTH = 100

_WIDE_ONLY_COLUMNS = frozenset({"stage", "cons", "complexity", "now"})
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
_QUEUE_READY_CAP = 50
_COLOR_GREEN = "#7ee787"
_COLOR_YELLOW = "#e3b341"
_COLOR_RED = "#f85149"
_COLOR_CYAN = "#56b6c2"
_COLOR_MAGENTA = "#d2a8ff"
_COLOR_DIM = "#8b949e"
_PAGE = "#0a0d12"


def column_tier(name: str) -> str:
    """Return the width tier for a column key: always, comfortable, or wide."""
    return _COLUMN_TIERS.get(name, "always")


def column_align(name: str) -> str:
    """Horizontal alignment for a runs-table column key."""
    return "right" if name in RIGHT_ALIGNED else "left"


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


_PANEL_TITLES_ASCII: dict[str, str] = {
    "stats": "STATS",
    "limits": "LIMITS",
    "runs": "RUNS",
    "detail": "DETAIL",
}
_PANEL_ICON_ROLES: dict[str, str] = {
    "stats": "alloy",
    "limits": "limits",
    "runs": "runs",
    "detail": "detail",
}


def panel_border_title(panel_id: str, mode: str) -> str:
    """Left segment of a panel's top border: plain uppercase titles in ascii."""
    if mode == "ascii":
        return _PANEL_TITLES_ASCII[panel_id]
    role = _PANEL_ICON_ROLES[panel_id]
    return f"{icon(role, mode)} {panel_id}"


def panel_border_subtitle(
    panel_id: str, snapshot: dict[str, Any], *, mode: str
) -> str | None:
    """Right segment of a panel's top border when a count or status applies."""
    if panel_id == "runs":
        count = len(snapshot.get("runs") or [])
        noun = "run" if count == 1 else "runs"
        return f"{count} {noun}"
    return None


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


def run_rows(snapshot: dict[str, Any], mode: str | None = None) -> list[tuple[str, ...]]:
    """One tuple per `runs[]` entry, in `COLUMNS` order."""
    return [_row(run, mode) for run in snapshot.get("runs") or []]


@dataclass(frozen=True)
class TreeRow:
    """One row in the monitor task tree (QUEUE, epics, runs, queue children)."""

    key: str
    kind: str
    depth: int
    cells: dict[str, str | Text]


def task_tree_rows(
    snapshot: dict[str, Any],
    expanded: set[str],
    width: int,
    *,
    mode: str | None = None,
) -> list[TreeRow]:
    """Build collapsible task-tree rows from a monitor snapshot."""
    _ = width  # column filtering is applied by the view; cells always carry all keys
    rows: list[TreeRow] = []
    queue = snapshot.get("queue") or {}
    ready = queue.get("ready") or []
    blocked = queue.get("blocked") or []
    ready_total = int(queue.get("ready_total") or 0)
    runs = snapshot.get("runs") or []

    queue_open = "queue" in expanded
    show_queue = queue_open or ready_total > 0 or bool(blocked)
    if show_queue:
        toggle = "▾" if queue_open else "▸"
        next_id = ready[0]["bead_id"] if ready else "-"
        rows.append(
            TreeRow(
                key="queue",
                kind="queue",
                depth=0,
                cells=_blank_cells(
                    bead=_queue_bead_label(toggle, mode),
                    status=f"{ready_total} ready · {len(blocked)} blocked · next: {next_id}",
                ),
            )
        )
    if queue_open:
        for index, bead in enumerate(ready, start=1):
            rows.append(_queued_row(bead, queue_index=index, depth=1, mode=mode))
        if ready_total > _QUEUE_READY_CAP:
            more = ready_total - _QUEUE_READY_CAP
            rows.append(
                TreeRow(
                    key="queue/more",
                    kind="more",
                    depth=1,
                    cells=_blank_cells(status=f"… {more} more"),
                )
            )
        for bead in blocked:
            rows.append(_blocked_row(bead, depth=1))

    for epic in snapshot.get("epics") or []:
        epic_id = epic["epic_id"]
        epic_key = f"epic/{epic_id}"
        epic_open = epic_key in expanded
        epic_toggle = "▾" if epic_open else "▸"
        cells = _blank_cells(
            bead=_epic_bead_label(epic, epic_toggle, mode),
            status=(
                f"{epic.get('running', 0)} running · {epic.get('judge', 0)} judge · "
                f"{epic.get('done', 0)}/{epic.get('total', 0)} done"
            ),
        )
        if mode == "nerd":
            cells["iter"] = f"{epic.get('done', 0)}/{epic.get('total', 0)}"
        rows.append(
            TreeRow(
                key=epic_key,
                kind="epic",
                depth=0,
                cells=cells,
            )
        )
        if epic_open:
            children: list[TreeRow] = []
            for run in runs:
                if run.get("epic_id") == epic_id:
                    children.append(_run_tree_row(run, depth=1, mode=mode))
            for bead in ready:
                if bead.get("epic_id") == epic_id:
                    children.append(_queued_row(bead, depth=1, mode=mode))
            if epic.get("done", 0):
                children.append(_done_fold_row(epic, depth=1))
            rows.extend(_prefix_tree_children(children))

    for run in runs:
        if run.get("epic_id") is None:
            rows.append(_run_tree_row(run, depth=0, mode=mode))

    return rows


def _blank_cells(**overrides: str | Text) -> dict[str, str | Text]:
    cells: dict[str, str | Text] = {name: "-" for name in COLUMNS}
    cells.update(overrides)
    return cells


def _queue_bead_label(toggle: str, mode: str | None) -> str | Text:
    if mode != "nerd":
        return f"{toggle} QUEUE"
    label = Text()
    label.append(f"{toggle} ")
    label.append(icon("queue", "nerd"))
    label.append(" QUEUE")
    return label


def _epic_bead_label(epic: dict[str, Any], toggle: str, mode: str | None) -> str | Text:
    epic_id = epic["epic_id"]
    title = epic.get("title", "")
    if mode != "nerd":
        return f"{toggle} {epic_id}  {title}"
    label = Text()
    label.append(f"{toggle} ")
    label.append(icon("folder", "nerd"), style=_COLOR_CYAN)
    label.append(" ")
    label.append(epic_id, style=f"bold {_COLOR_CYAN}")
    if title:
        label.append(f"  {title}", style=_COLOR_DIM)
    return label


def _cells_from_run(run: dict[str, Any], mode: str | None = None) -> dict[str, str | Text]:
    return dict(zip(COLUMNS, _row(run, mode)))


def _prefix_tree_children(children: list[TreeRow]) -> list[TreeRow]:
    if not children:
        return []
    glyphs = ["├─"] * (len(children) - 1) + ["└─"]
    prefixed: list[TreeRow] = []
    for glyph, child in zip(glyphs, children):
        cells = dict(child.cells)
        bead = cells["bead"]
        if isinstance(bead, Text):
            marked = Text(f"{glyph} ")
            marked.append_text(bead)
            cells["bead"] = marked
        else:
            cells["bead"] = f"{glyph} {bead}"
        prefixed.append(
            TreeRow(key=child.key, kind=child.kind, depth=child.depth, cells=cells)
        )
    return prefixed


def _run_tree_row(run: dict[str, Any], depth: int, mode: str | None = None) -> TreeRow:
    return TreeRow(
        key=f"run/{run['run_id']}",
        kind="run",
        depth=depth,
        cells=_cells_from_run(run, mode),
    )


def _queued_row(
    bead: dict[str, Any],
    *,
    queue_index: int | None = None,
    depth: int,
    mode: str | None = None,
) -> TreeRow:
    bead_id = bead["bead_id"]
    tests = f"queued #{queue_index}" if queue_index is not None else "-"
    return TreeRow(
        key=f"queue/{bead_id}",
        kind="queued",
        depth=depth,
        cells=_blank_cells(
            bead=bead_id,
            recipe=_text(bead.get("recipe")),
            status="ready",
            tests=tests,
            complexity=_complexity(bead.get("complexity"), mode),
        ),
    )


def _blocked_row(bead: dict[str, Any], depth: int) -> TreeRow:
    blockers = ", ".join(bead.get("blocked_by") or [])
    return TreeRow(
        key=f"queue/{bead['bead_id']}",
        kind="blocked",
        depth=depth,
        cells=_blank_cells(
            bead=f"⊘ {bead['bead_id']}",
            status=f"by {blockers}",
        ),
    )


def _done_fold_row(epic: dict[str, Any], depth: int) -> TreeRow:
    epic_id = epic["epic_id"]
    done_ids = " ".join(epic.get("done_ids") or [])
    summary = f"✓ {epic.get('done', 0)} done"
    if done_ids:
        summary = f"{summary}  ({done_ids})"
    return TreeRow(
        key=f"epic/{epic_id}/done",
        kind="done_fold",
        depth=depth,
        cells=_blank_cells(bead=summary),
    )


def status_color(status: str) -> str:
    """Map a run status label to its DataTable badge color."""
    return {
        "running": _COLOR_CYAN,
        "judge": _COLOR_MAGENTA,
        "blocked": _COLOR_RED,
        "done": _COLOR_GREEN,
        "ready": _COLOR_DIM,
    }.get(status, _COLOR_DIM)


def status_badge(status: str, mode: str) -> Text:
    """Status cell: nerd pill caps, unicode icon+word, or ascii colored word."""
    color = status_color(status)
    if mode == "ascii":
        return Text(status, style=color)
    if mode == "unicode":
        return Text(f"{icon(status, mode)} {status}", style=color)
    inner = f"{icon(status, mode)} {status}"
    badge = Text()
    badge.append(icon("pill_l", mode), style=color)
    badge.append(inner, style=f"bold {_PAGE} on {color}")
    badge.append(icon("pill_r", mode), style=color)
    return badge


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


def _row(run: dict[str, Any], mode: str | None = None) -> tuple[str, ...]:
    return (
        _text(run.get("bead_id")),
        _text(run.get("recipe")),
        _text(run.get("status")),
        _text(run.get("stage")),
        f"{_text(run.get('iteration'))}/{_text(run.get('max_iterations'))}",
        f"{_text(run.get('consiliums'))}/{_text(run.get('max_consiliums'))}",
        _tests(run, mode),
        _elapsed(run.get("elapsed_minutes")),
        _now(run.get("current_calls") or []),
        _tokens(run.get("tokens") or {}),
        _judge(run.get("judge")),
        _complexity(run.get("complexity"), mode),
    )


def _text(value: Any) -> str:
    return "-" if value is None else str(value)


_COMPLEXITY_BARS = {
    "simple": "▂",
    "medium": "▂▄",
    "complex": "▂▄▆",
}


def _complexity(value: Any, mode: str | None = None) -> str:
    if mode is None or mode == "ascii":
        return _text(value)
    if value is None:
        return "-"
    return _COMPLEXITY_BARS.get(str(value), "-")


def _tests(run: dict[str, Any], mode: str | None = None) -> str:
    """`n checks` once the verification loop has run anything, else the ledger's
    tests summary verbatim (ascii), or tick/cross glyphs in nerd/unicode."""
    checks = run.get("checks")
    if isinstance(checks, dict) and checks.get("total") is not None:
        if mode is None or mode == "ascii":
            return f"{checks['total']} checks"
        total = checks["total"]
        last = checks.get("last") or {}
        glyph = "test_ok" if last.get("exit_code") == 0 else "test_fail"
        return f"{icon(glyph, mode)}{total}"
    summary = run.get("tests_summary")
    if mode is None or mode == "ascii":
        return _text(summary)
    if summary is None:
        return "-"
    passed, failed = parse_counts(str(summary))
    if passed is None and failed is None:
        return str(summary)
    parts: list[str] = []
    if passed is not None:
        parts.append(f"{icon('test_ok', mode)}{passed}")
    if failed is not None:
        parts.append(f"{icon('test_fail', mode)}{failed}")
    return " ".join(parts)


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

_DETAIL_TOKEN_BAR_WIDTH = 8


def detail_panel_border_title(run: dict[str, Any]) -> str:
    """Detail panel top-border title: ``<bead id> · <title>`` for the selected run."""
    bead_id = _text(run.get("bead_id"))
    title = run.get("title")
    if title:
        return f"{bead_id} · {title}"
    return bead_id


def detail_panel_border_title_queue() -> str:
    return "QUEUE"


def detail_panel_border_title_epic(epic: dict[str, Any]) -> str:
    epic_id = _text(epic.get("epic_id"))
    title = epic.get("title")
    if title:
        return f"{epic_id} · {title}"
    return epic_id


def detail_panel_border_subtitle(run: dict[str, Any]) -> str:
    """Detail panel top-border subtitle: ``<status> <stage>`` for the selected run."""
    return f"{_text(run.get('status'))} {_text(run.get('stage'))}"


def detail_panel_border_subtitle_queue(snapshot: dict[str, Any]) -> str:
    queue = snapshot.get("queue") or {}
    ready_total = int(queue.get("ready_total") or 0)
    blocked = queue.get("blocked") or []
    return f"{ready_total} ready · {len(blocked)} blocked"


def detail_panel_border_subtitle_epic(epic: dict[str, Any]) -> str:
    done = int(epic.get("done") or 0)
    total = int(epic.get("total") or 0)
    return f"{done}/{total} done"


def queue_detail(snapshot: dict[str, Any], *, mode: str | None = None) -> list[str]:
    """Detail pane for the QUEUE row: dispatch note and ready/blocked counts."""
    queue = snapshot.get("queue") or {}
    ready_total = int(queue.get("ready_total") or 0)
    blocked = queue.get("blocked") or []
    if mode in ("nerd", "unicode"):
        return [
            f"{icon('queue', mode)} dispatch  priority, then age.",
            f"{icon('ready', mode)} ready  {ready_total}",
            f"{icon('blocked', mode)} blocked  {len(blocked)}",
        ]
    return [
        "Dispatch order: priority, then age.",
        f"{ready_total} ready",
        f"{len(blocked)} blocked",
    ]


def epic_detail(epic: dict[str, Any], *, mode: str | None = None) -> list[str]:
    """Detail pane for an epic row: title, progress, running/judge counts, done ids."""
    title = epic.get("title") or epic.get("epic_id") or ""
    done = int(epic.get("done") or 0)
    total = int(epic.get("total") or 0)
    running = int(epic.get("running") or 0)
    judge = int(epic.get("judge") or 0)
    if mode in ("nerd", "unicode"):
        lines = [
            f"{icon('folder', mode)} title  {title}",
            f"{icon('done', mode)} progress  {done}/{total} done",
            (
                f"{icon('running', mode)} active  {running} running"
                f" · {judge} judge"
            ),
        ]
        lines.extend(epic.get("done_ids") or [])
        return lines
    lines = [
        title,
        f"{done}/{total} done",
        f"{running} running · {judge} judge",
    ]
    lines.extend(epic.get("done_ids") or [])
    return lines


def format_detail(
    run: dict[str, Any],
    width: int,
    log_dir: str | None = None,
    *,
    mode: str | None = None,
) -> str:
    """Render detail pane text: two columns at comfortable width, stacked below."""
    if mode in ("nerd", "unicode"):
        return _format_detail_styled(run, width, log_dir, mode)
    lines = detail_lines(run, log_dir, mode=mode)
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


def _format_detail_styled(
    run: dict[str, Any],
    width: int,
    log_dir: str | None,
    mode: str,
) -> str:
    left = _detail_left_column(run, mode)
    right = _detail_right_column(run, log_dir, mode)
    if width < COMFORTABLE_WIDTH:
        return "\n".join(left + right)
    left_width = max((len(line) for line in left), default=0)
    row_count = max(len(left), len(right))
    rows: list[str] = []
    for index in range(row_count):
        left_line = left[index] if index < len(left) else ""
        right_line = right[index] if index < len(right) else ""
        if right_line:
            rows.append(f"{left_line:<{left_width}}  {right_line}" if left_width else right_line)
        else:
            rows.append(left_line)
    return "\n".join(rows)


def _detail_left_column(run: dict[str, Any], mode: str) -> list[str]:
    lines: list[str] = []
    for call in run.get("current_calls") or []:
        requested = _runner(call.get("requested_runner"), call.get("requested_model"))
        effective = _runner(call.get("effective_runner"), call.get("effective_model"))
        seconds = call.get("elapsed_seconds")
        elapsed = "-" if seconds is None else f"{int(seconds)}s"
        lines.append(
            f"{icon('running', mode)} verify  {requested} -> {effective} ({elapsed})"
        )
    judge_line = _judge_detail(run.get("judge"))
    if judge_line is not None:
        lines.append(f"{icon('judge', mode)} {judge_line.replace('judge:', 'judge  ', 1)}")
    tokens_by_role = run.get("tokens_by_role") or {}
    if tokens_by_role:
        total_in = sum(entry.get("input_tokens") or 0 for entry in tokens_by_role.values())
        total_out = sum(entry.get("output_tokens") or 0 for entry in tokens_by_role.values())
        lines.append(
            f"{icon('tokens', mode)} tokens  in {total_in}  out {total_out}"
        )
        max_tokens = max((entry.get("total_tokens") or 0 for entry in tokens_by_role.values()), default=0)
        for role, entry in tokens_by_role.items():
            total = entry.get("total_tokens") or 0
            bar = _detail_role_token_bar(total, max_tokens)
            lines.append(f"   {role}  {total}  {bar}".rstrip())
    for entry in run.get("models_used") or []:
        lines.append(f"{icon('complexity', mode)} {_models_used_line(entry)}")
    return lines


def _detail_right_column(run: dict[str, Any], log_dir: str | None, mode: str) -> list[str]:
    lines: list[str] = []
    complexity = run.get("complexity")
    if complexity is not None:
        bar = _complexity(complexity, mode)
        estimated = run.get("complexity_estimated")
        suffix = " (est.)" if estimated else ""
        lines.append(f"cx  {bar} {complexity}{suffix}")
    lines.append(f"worktree {_text(run.get('worktree'))}")
    lines.append(f"branch {_text(run.get('branch'))}")
    if log_dir is not None:
        lines.append(f"logs {log_dir}")
    parent = run.get("parent_id") or run.get("epic_id")
    if parent:
        lines.append(f"parent {_text(parent)}")
    return lines


def _detail_role_token_bar(tokens: int, max_tokens: int) -> str:
    if max_tokens <= 0 or tokens <= 0:
        return ""
    filled = max(1, round(tokens / max_tokens * _DETAIL_TOKEN_BAR_WIDTH))
    return "█" * min(filled, _DETAIL_TOKEN_BAR_WIDTH)


def detail_lines(
    run: dict[str, Any],
    log_dir: str | None = None,
    *,
    mode: str | None = None,
) -> list[str]:
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
