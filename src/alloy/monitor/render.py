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

COLUMNS = ("bead", "status", "stage", "iter", "cons", "tests", "elapsed", "now", "tokens", "judge", "complexity")

RIGHT_ALIGNED = frozenset({"iter", "cons", "tests", "elapsed", "tokens"})

COMFORTABLE_WIDTH = 80
WIDE_WIDTH = 100

_WIDE_ONLY_COLUMNS = frozenset({"stage", "cons", "complexity", "now"})
_COLUMN_TIERS: dict[str, str] = {name: "wide" for name in _WIDE_ONLY_COLUMNS}

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
    return tuple(name for name in COLUMNS if name not in _WIDE_ONLY_COLUMNS)


def header_line(snapshot: dict[str, Any], mode: str | None = None) -> str:
    """One-line stats summary: scheduler, ready queue and lifetime totals."""
    if mode is None or mode == "ascii":
        return _header_line_ascii(snapshot)
    if mode == "nerd":
        return _header_line_styled(snapshot, "nerd")
    return _header_line_styled(snapshot, "unicode")


def activity_line(snapshot: dict[str, Any]) -> str:
    """Current model and action, including calls outside bead runs."""
    activities: list[str] = []
    for call in snapshot.get("auxiliary_calls") or []:
        model = call.get("effective_model") or call.get("effective_runner") or "unknown"
        action = (
            "reviewing repository memory"
            if call.get("role") == "memory_reviewer"
            else str(call.get("role") or "working").replace("_", " ")
        )
        activities.append(f"{model}: {action}")
    for run in snapshot.get("runs") or []:
        if run.get("status") != "running":
            continue
        bead_suffix = _bead_recipe_suffix(run)
        calls = run.get("current_calls") or []
        if calls:
            for call in calls:
                model = call.get("effective_model") or call.get("effective_runner") or "unknown"
                action = str(call.get("role") or run.get("stage") or "working").replace("_", " ")
                activities.append(f"{model}: {action} {bead_suffix}")
        else:
            action = str(run.get("stage") or "working").replace("_", " ")
            activities.append(f"none: {action} {bead_suffix}")
    return "; ".join(activities) if activities else "none: idle"


def _bead_recipe_suffix(run: dict[str, Any]) -> str:
    bead_id = run.get("bead_id")
    recipe = run.get("recipe")
    if recipe is None:
        return f"({bead_id})"
    return f"({bead_id} · {recipe})"


def _header_line_ascii(snapshot: dict[str, Any]) -> str:
    scheduler = snapshot.get("scheduler") or {}
    if scheduler.get("running"):
        sched = f"scheduler running (pid {scheduler.get('pid')})"
    else:
        sched = "scheduler stopped"
    ready = f"ready {snapshot.get('ready_count', 0)} (capped at {snapshot.get('ready_capped_at')})"
    lifetime = snapshot.get("lifetime") or {}
    totals = (
        f"done {lifetime.get('done', 0)}  failed {lifetime.get('failed', 0)}  cancelled {lifetime.get('cancelled', 0)}"
    )
    line = f"{sched}  |  {ready}  |  {totals}"
    if "session_totals" in snapshot:
        session = snapshot["session_totals"] or {}
        line += (
            f"  |  this session: done {session.get('done', 0)}"
            f" failed {session.get('failed', 0)} cancelled {session.get('cancelled', 0)}"
        )
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


def panel_border_subtitle(panel_id: str, snapshot: dict[str, Any], *, mode: str) -> str | None:
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
        f"{icon('arrow_left', mode)} {icon('refresh', mode)} 1s {icon('arrow_left', mode)} {icon('clock', mode)} {now} "
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
                    status=(f"{ready_total} ready · {len(blocked)} blocked\nnext: {next_id}"),
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
                f"{epic.get('running', 0)} running · {epic.get('judge', 0)} judge\n"
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
                    children.append(_queued_row(bead, depth=1, epic_scope=epic_id, mode=mode))
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
    if mode != "nerd":
        return f"{toggle} {epic_id}"
    label = Text()
    label.append(f"{toggle} ")
    label.append(icon("folder", "nerd"), style=_COLOR_CYAN)
    label.append(" ")
    label.append(epic_id, style=f"bold {_COLOR_CYAN}")
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
        prefixed.append(TreeRow(key=child.key, kind=child.kind, depth=child.depth, cells=cells))
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
    epic_scope: str | None = None,
    depth: int,
    mode: str | None = None,
) -> TreeRow:
    bead_id = bead["bead_id"]
    tests = f"queued #{queue_index}" if queue_index is not None else "-"
    if epic_scope is not None:
        row_key = f"epic/{epic_scope}/queued/{bead_id}"
    else:
        row_key = f"queue/{bead_id}"
    return TreeRow(
        key=row_key,
        kind="queued",
        depth=depth,
        cells=_blank_cells(
            bead=bead_id,
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
    # The full done-id list lives in the epic's detail pane (epic_detail());
    # naming ids here would widen the bead column for a row that never uses
    # its other columns.
    summary = f"✓ {epic.get('done', 0)} done"
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
        "failed": _COLOR_RED,
        "waiting-human": _COLOR_MAGENTA,
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


def limits_lines(
    snapshot: dict[str, Any],
    mode: str | None = None,
    width: int | None = None,
    usage_style: str = "remaining",
) -> list[str]:
    """One line per harness in HARNESSES order; empty when top-level limits is {}.

    In nerd/unicode mode below WIDE_WIDTH, each harness's second and later
    windows wrap onto their own continuation line indented under the first.
    """
    limits = snapshot.get("limits") or {}
    if not limits:
        return []
    reset_width = max(
        (
            len(suffix) + 2
            for sample in limits.values()
            for win in (sample.get("windows") or [])
            if (suffix := _reset_suffix(win.get("label"), win.get("resets_at")))
        ),
        default=0,
    )
    lines: list[str] = []
    for harness in HARNESSES:
        if harness in limits:
            lines.extend(
                _limits_line(
                    harness,
                    limits[harness],
                    mode=mode,
                    width=width,
                    usage_style=usage_style,
                    reset_width=reset_width,
                )
            )
    return lines


def _limits_harness_label(harness: str) -> str:
    """Fixed-width harness column so the first usage bar lines up across agents."""
    return harness.ljust(LIMITS_HARNESS_WIDTH)


def _limits_line(
    harness: str,
    sample: dict[str, Any],
    *,
    mode: str | None = None,
    width: int | None = None,
    usage_style: str = "remaining",
    reset_width: int = 0,
) -> list[str]:
    label = _limits_harness_label(harness)
    styled = mode in ("nerd", "unicode")
    if not sample.get("available"):
        error = _text(sample.get("error"))
        if styled:
            badge = _pill_badge(f"{icon('blocked', mode)} unavailable", _COLOR_RED, mode)
            line = f"{label}  {badge} [{_COLOR_RED}]{error}[/]"
        else:
            line = f"{label}  [{_COLOR_RED}]unavailable: {error}[/]"
        status = sample.get("status")
        if status:
            line += f" [{status}]"
        return [line]
    windows = sample.get("windows") or []
    segments = [
        _limits_window_segment(
            win,
            align_bar=True,
            mode=mode,
            usage_style=usage_style,
            reset_width=reset_width,
        )
        for index, win in enumerate(windows)
    ]
    wrap = styled and width is not None and width < WIDE_WIDTH and len(segments) > 1
    if wrap:
        indent = " " * (LIMITS_HARNESS_WIDTH + 2)
        lines = [f"{label}  {segments[0]}"]
        lines.extend(f"{indent}{segment}" for segment in segments[1:])
    else:
        lines = ["  ".join([label, *segments])]
    stale = _stale_as_of(sample.get("as_of"), mode=mode)
    if stale:
        lines[-1] += stale
    status = sample.get("status")
    if status:
        lines[-1] += f" [{status}]"
    return lines


def _usage_color(percent: int) -> str:
    if percent >= 80:
        return _COLOR_RED
    if percent >= 50:
        return _COLOR_YELLOW
    return _COLOR_GREEN


def _display_usage_percent(win: dict[str, Any], usage_style: str) -> int:
    used = int(win["used_percent"])
    return 100 - used if usage_style == "remaining" else used


_EIGHTH_BLOCKS = "▏▎▍▌▋▊▉"


def _usage_bar(percent: int, mode: str | None = None) -> str:
    if mode is None or mode == "ascii":
        filled = max(0, min(_USAGE_BAR_WIDTH, round(percent * _USAGE_BAR_WIDTH / 100)))
        return f"[{'█' * filled}{'░' * (_USAGE_BAR_WIDTH - filled)}]"
    eighths = max(0, min(_USAGE_BAR_WIDTH * 8, round(percent * _USAGE_BAR_WIDTH * 8 / 100)))
    full, part = divmod(eighths, 8)
    bar = "█" * full + (_EIGHTH_BLOCKS[part - 1] if part else "")
    return bar + "░" * (_USAGE_BAR_WIDTH - full - (1 if part else 0))


def _pill_badge(text: str, color: str, mode: str) -> str:
    """Textual-markup pill: colored caps around a bold on-color label."""
    return f"[{color}]{icon('pill_l', mode)}[/][bold {_PAGE} on {color}]{text}[/][{color}]{icon('pill_r', mode)}[/]"


def _limits_window_head(label: str, percent: int, *, align_bar: bool) -> str:
    if align_bar:
        return f"{label.ljust(LIMITS_WINDOW_LABEL_WIDTH)} {percent:>3}%"
    return f"{label} {percent}%"


def _limits_window_segment(
    win: dict[str, Any],
    *,
    align_bar: bool = False,
    mode: str | None = None,
    usage_style: str = "remaining",
    reset_width: int = 0,
) -> str:
    used = int(win["used_percent"])
    percent = 100 - used if usage_style == "remaining" else used
    color = _usage_color(used)
    head = _limits_window_head(win["label"], percent, align_bar=align_bar)
    segment = f"[{color}]{head} {_usage_bar(percent, mode)}[/]"
    styled = mode in ("nerd", "unicode")
    warning = styled and used >= 80
    if styled:
        warning_icon = icon("warn", mode) if warning else " "
        segment += f" {warning_icon}"
    reset_suffix = _reset_suffix(win.get("label"), win.get("resets_at"))
    if reset_width:
        reset = ""
        if reset_suffix:
            if styled:
                glyph = icon("clock" if ":" in reset_suffix else "calendar", mode)
                reset = f"{glyph} {reset_suffix}"
            else:
                reset = f"({reset_suffix})"
        segment += f" {reset:>{reset_width}}"
    elif reset_suffix:
        if styled:
            glyph = icon("clock" if ":" in reset_suffix else "calendar", mode)  # HH:MM vs "Sep 25"
            segment += f" {glyph} {reset_suffix}"
        else:
            segment += f" ({reset_suffix})"
    if win.get("stale"):
        if styled:
            badge = _pill_badge(icon("stale", mode) + " stale", _COLOR_YELLOW, mode)
            segment += f" {badge}"
        else:
            segment += f" [{_COLOR_YELLOW}][stale][/]"
    return segment


def _row(run: dict[str, Any], mode: str | None = None) -> tuple[str, ...]:
    return (
        _text(run.get("bead_id")),
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


_METADATA_PREFIXES = ("bead:", "branch:")

_DETAIL_TOKEN_BAR_WIDTH = 8


def detail_panel_border_title(run: dict[str, Any]) -> str:
    """Detail panel top-border title: ``<bead id> · <title>`` for the selected run."""
    return _text(run.get("bead_id"))


def detail_panel_border_title_queue() -> str:
    return "QUEUE"


def detail_panel_border_title_epic(epic: dict[str, Any]) -> str:
    return _text(epic.get("epic_id"))


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
    """Detail pane for an epic row: progress, running/judge counts, done ids, title last."""
    title = epic.get("title") or epic.get("epic_id") or ""
    done = int(epic.get("done") or 0)
    total = int(epic.get("total") or 0)
    running = int(epic.get("running") or 0)
    judge = int(epic.get("judge") or 0)
    if mode in ("nerd", "unicode"):
        lines = [
            f"{icon('done', mode)} progress  {done}/{total} done",
            f"{icon('running', mode)} active  {running} running · {judge} judge",
        ]
        lines.extend(epic.get("done_ids") or [])
        lines.append(f"{icon('folder', mode)} title  {title}")
        return lines
    lines = [
        f"{done}/{total} done",
        f"{running} running · {judge} judge",
    ]
    lines.extend(epic.get("done_ids") or [])
    lines.append(f"title: {title}")
    return lines


def queued_detail(bead: dict[str, Any], *, mode: str | None = None) -> list[str]:
    """Detail pane for a ready/blocked bead that has no run yet: metadata, title last."""
    lines = [f"bead: {_text(bead.get('bead_id'))}"]
    if bead.get("blocked_by"):
        lines.append(f"blocked by: {', '.join(bead['blocked_by'])}")
    else:
        lines.append("status: ready")
    for key in ("recipe", "complexity", "epic_id"):
        if bead.get(key):
            lines.append(f"{key.replace('_', ' ')}: {bead[key]}")
    lines.append(f"title: {_text(bead.get('title'))}")
    return lines


def _token_table(tokens_by_role: dict[str, Any], indent: str = "") -> list[str]:
    """Aligned per-role token table with a header row (total = in + out)."""
    rows = [
        (
            role,
            entry.get("total_tokens") or 0,
            entry.get("input_tokens") or 0,
            entry.get("output_tokens") or 0,
        )
        for role, entry in tokens_by_role.items()
    ]
    name_w = max([4] + [len(r[0]) for r in rows])
    num_w = max([5] + [len(str(v)) for r in rows for v in r[1:]])
    max_total = max((r[1] for r in rows), default=0)
    lines = [f"{indent}{'role':<{name_w}}  {'total':>{num_w}}  {'in':>{num_w}}  {'out':>{num_w}}  share"]
    for role, total, inp, out in rows:
        bar = _detail_role_token_bar(total, max_total)
        lines.append(f"{indent}{role:<{name_w}}  {total:>{num_w}}  {inp:>{num_w}}  {out:>{num_w}}  {bar}".rstrip())
    return lines


def _models_table(entries: list[dict[str, Any]], indent: str = "", usage_style: str = "remaining") -> list[str]:
    """Aligned models-used table: model, calls, tokens, then limit windows."""
    rows = []
    for entry in entries:
        runner, model = entry.get("runner"), entry.get("model")
        label = f"{runner}:{model}" if model else _text(runner)
        windows = "  ".join(
            f"{win['label']} {_display_usage_percent(win, usage_style)}%"
            for win in (entry.get("windows") or {}).values()
        )
        rows.append((label, str(entry.get("calls", 0)), _text(entry.get("total_tokens")), windows))
    if not rows:
        return []
    model_w = max([5] + [len(r[0]) for r in rows])
    calls_w = max([5] + [len(r[1]) for r in rows])
    tok_w = max([6] + [len(r[2]) for r in rows])
    lines = [f"{indent}{'model':<{model_w}}  {'calls':>{calls_w}}  {'tokens':>{tok_w}}  limits"]
    for label, calls, tokens, windows in rows:
        lines.append(f"{indent}{label:<{model_w}}  {calls:>{calls_w}}  {tokens:>{tok_w}}  {windows}".rstrip())
    return lines


def format_detail(
    run: dict[str, Any],
    width: int,
    log_dir: str | None = None,
    *,
    mode: str | None = None,
    usage_style: str = "remaining",
) -> str:
    """Render detail pane text: two columns at comfortable width, stacked below."""
    if mode in ("nerd", "unicode"):
        return _format_detail_styled(run, width, log_dir, mode, usage_style)
    lines = detail_lines(run, log_dir, mode=mode, usage_style=usage_style)
    title_line, lines = lines[0], lines[1:]
    if width < COMFORTABLE_WIDTH:
        return "\n".join([title_line, *lines])
    activity = [line for line in lines if not line.startswith(_METADATA_PREFIXES)]
    metadata = [line for line in lines if line.startswith(_METADATA_PREFIXES)]
    left_width = max((len(line) for line in activity), default=0)
    row_count = max(len(activity), len(metadata))
    rows: list[str] = [title_line]
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
    usage_style: str = "remaining",
) -> str:
    title_line = f"{icon('folder', mode)} title  {_text(run.get('title'))}"
    left = _detail_left_column(run, mode, usage_style)
    right = _detail_right_column(run, log_dir, mode)
    if width < COMFORTABLE_WIDTH:
        return "\n".join([title_line, *left, *right])
    left_width = max((len(line) for line in left), default=0)
    row_count = max(len(left), len(right))
    rows: list[str] = [title_line]
    for index in range(row_count):
        left_line = left[index] if index < len(left) else ""
        right_line = right[index] if index < len(right) else ""
        if right_line:
            rows.append(f"{left_line:<{left_width}}  {right_line}" if left_width else right_line)
        else:
            rows.append(left_line)
    return "\n".join(rows)


def _detail_left_column(run: dict[str, Any], mode: str, usage_style: str = "remaining") -> list[str]:
    lines: list[str] = []
    for call in run.get("current_calls") or []:
        requested = _runner(call.get("requested_runner"), call.get("requested_model"))
        effective = _runner(call.get("effective_runner"), call.get("effective_model"))
        seconds = call.get("elapsed_seconds")
        elapsed = "-" if seconds is None else f"{int(seconds)}s"
        lines.append(f"{icon('running', mode)} verify  {requested} -> {effective} ({elapsed})")
    judge_line = _judge_detail(run.get("judge"))
    if judge_line is not None:
        lines.append(f"{icon('judge', mode)} {judge_line.replace('judge:', 'judge  ', 1)}")
    tokens_by_role = run.get("tokens_by_role") or {}
    if tokens_by_role:
        lines.append(f"{icon('tokens', mode)} tokens per role (total = in + out)")
        lines.extend(_token_table(tokens_by_role, indent="   "))
    models = run.get("models_used") or []
    if models:
        lines.append(f"{icon('complexity', mode)} models used")
        lines.extend(_models_table(models, indent="   ", usage_style=usage_style))
    return lines


def _detail_right_column(run: dict[str, Any], log_dir: str | None, mode: str) -> list[str]:
    lines: list[str] = []
    complexity = run.get("complexity")
    if complexity is not None:
        bar = _complexity(complexity, mode)
        estimated = run.get("complexity_estimated")
        suffix = " (est.)" if estimated else ""
        lines.append(f"cx  {bar} {complexity}{suffix}")
    lines.append(f"branch {_text(run.get('branch'))}")
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
    usage_style: str = "remaining",
) -> list[str]:
    """Selected-run detail pane: in-flight calls, raw vs effective judge, per-role tokens, paths.

    `log_dir` is not part of the run dict (it is `{root}/logs/{run_id}`), so the
    caller passes it in; it is omitted when None.
    """
    lines: list[str] = [f"title: {_text(run.get('title'))}"]
    for call in run.get("current_calls") or []:
        requested = _runner(call.get("requested_runner"), call.get("requested_model"))
        effective = _runner(call.get("effective_runner"), call.get("effective_model"))
        seconds = call.get("elapsed_seconds")
        elapsed = "-" if seconds is None else f"{int(seconds)}s"
        lines.append(f"{_text(call.get('role'))}: {requested} -> {effective} ({elapsed})")
    judge_line = _judge_detail(run.get("judge"))
    if judge_line is not None:
        lines.append(judge_line)
    tokens_by_role = run.get("tokens_by_role") or {}
    if tokens_by_role:
        lines.append("tokens per role (total = in + out):")
        lines.extend(_token_table(tokens_by_role, indent="  "))
    models = run.get("models_used") or []
    if models:
        lines.append("models used:")
        lines.extend(_models_table(models, indent="  ", usage_style=usage_style))
    lines.append(f"bead: {_text(run.get('bead_id'))}")
    lines.append(f"branch: {_text(run.get('branch'))}")
    return lines


def _runner(runner: Any, model: Any) -> str:
    return f"{_text(runner)}:{model}" if model else _text(runner)


def _models_used_line(entry: dict[str, Any], usage_style: str = "remaining") -> str:
    runner = entry.get("runner")
    model = entry.get("model")
    label = f"{runner}:{model}" if model else _text(runner)
    parts = [
        f"model {label}",
        f"calls {entry.get('calls', 0)}",
        f"tokens {_text(entry.get('total_tokens'))}",
    ]
    window_parts = [
        f"{win['label']} {_display_usage_percent(win, usage_style)}%" for win in (entry.get("windows") or {}).values()
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


def _reset_suffix(label: str | None, resets_at: str | None) -> str | None:
    parsed = _parse_iso(resets_at)
    if parsed is None:
        return None
    local = parsed.astimezone()
    if label == "5h":
        return local.strftime("%H:%M")
    if label == "cycle" or (label or "").startswith("weekly"):
        now_local = datetime.now().astimezone()
        if local.date() == now_local.date():
            return local.strftime("%H:%M")
        return f"{local.strftime('%b')} {local.day}"  # "Jul 19": no year, saves width
    return None


def _stale_as_of(as_of: str | None, mode: str | None = None) -> str:
    parsed = _parse_iso(as_of)
    if parsed is None:
        return ""
    age = (datetime.now(timezone.utc) - parsed).total_seconds()
    if age <= 30 * 60:
        return ""
    if mode in ("nerd", "unicode"):
        badge = _pill_badge(icon("stale", mode) + f" as of {parsed.strftime('%H:%M')}", _COLOR_YELLOW, mode)
        return f" {badge}"
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
    return (
        f"judge said {raw['decision']} (confidence {_text(raw.get('confidence'))})"
        f" / Alloy did {_text(effective.get('decision'))} -- {_text(effective.get('reason'))}"
    )
