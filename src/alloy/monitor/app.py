"""Textual live view for `alloy monitor`: stats line, runs table, detail pane, refresh, quit.

Strictly read-only: the app only ever calls `snapshot_source()` (off the event
loop, in a thread worker) and touches its own widgets. See "Component 3" in
docs/plans/execution-monitor.md.
"""

from __future__ import annotations

from typing import Any, Callable

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.events import Resize
from textual.widgets import DataTable, Footer, Header, Static

from alloy.monitor.icons import icon, resolve_mode
from alloy.monitor.render import (
    column_align,
    detail_panel_border_subtitle,
    detail_panel_border_subtitle_epic,
    detail_panel_border_subtitle_queue,
    detail_panel_border_title,
    detail_panel_border_title_epic,
    detail_panel_border_title_queue,
    epic_detail,
    format_detail,
    header_line,
    limits_lines,
    panel_border_subtitle,
    panel_border_title,
    queue_detail,
    status_badge,
    task_tree_rows,
    title_line,
    visible_columns,
)

_SELECTED_MARKER = "\u258c"


def _with_selected_marker(value: Text | str) -> Text:
    marked = Text(_SELECTED_MARKER)
    if isinstance(value, Text):
        marked.append_text(value)
    else:
        marked.append(str(value))
    return marked


def _without_selected_marker(value: Text | str) -> Text | str:
    if isinstance(value, Text) and value.plain.startswith(_SELECTED_MARKER):
        return value[1:]
    return value


class MonitorFooter(Footer):
    """Footer with key chips and a right-aligned ``icons: <mode>`` marker."""

    def render(self) -> Text:
        line = Text()
        for child in self.children:
            rendered = child.render()
            line.append(getattr(rendered, "plain", str(rendered)))
        mode = resolve_mode(interactive=True)
        keyboard = icon("keyboard", mode)
        if keyboard:
            line.append(f" {keyboard} icons: {mode} ")
        else:
            line.append(f" icons: {mode} ")
        return line


class MonitorApp(App[None]):
    """Live dashboard over a `snapshot_source` callable returning the frozen snapshot dict."""

    TITLE = "alloy monitor"
    CSS_PATH = "monitor.tcss"
    BINDINGS = [
        ("j,down", "cursor_down", "move"),
        ("k,up", "cursor_up", ""),
        ("enter,l", "toggle_detail", "Detail"),
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        snapshot_source: Callable[[], dict[str, Any]],
        interval: float = 1.0,
        limits_source: Callable[[], dict[str, Any]] | None = None,
        limits_interval: float = 60.0,
    ) -> None:
        super().__init__()
        self.snapshot_source = snapshot_source
        self.interval = interval
        self.limits_source = limits_source
        self.limits_interval = limits_interval
        self._snapshot: dict[str, Any] | None = None
        self._probed_limits: dict[str, Any] | None = None
        self._marked_row: int | None = None
        self._expanded: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        stats = Static("loading…", id="stats")
        stats.border_title = "STATS"
        yield stats
        limits = Static("", id="limits", markup=True)
        limits.border_title = "LIMITS"
        limits.display = False
        yield limits
        runs = DataTable(id="runs", cursor_type="row")
        runs.border_title = "RUNS"
        yield runs
        detail = Static("", id="detail")
        detail.border_title = "DETAIL"
        detail.display = False
        yield detail
        yield MonitorFooter(show_command_palette=False)

    def get_key_display(self, binding: Binding) -> str:
        if binding.action == "cursor_down":
            return "[j/k]"
        return super().get_key_display(binding)

    def on_mount(self) -> None:
        self._sync_runs_table_columns()
        self.refresh_snapshot()
        self.set_interval(self.interval, self.refresh_snapshot)
        if self.limits_source is not None:
            self.refresh_limits()
            self.set_interval(self.limits_interval, self.refresh_limits)

    def on_resize(self, event: Resize) -> None:
        if self._sync_runs_table_columns(event.size.width) and self._snapshot is not None:
            self.apply_snapshot(self._snapshot, width=event.size.width)

    @work(thread=True, exclusive=True)
    def refresh_snapshot(self) -> None:
        """Fetch a snapshot off the event loop and apply it; keep the last good one on failure."""
        try:
            snapshot = self.snapshot_source()
        except Exception as exc:  # noqa: BLE001 - any failure must leave the view alive
            self.log(f"refresh failed: {exc!r}")
            self.call_from_thread(self._show_failure, type(exc).__name__)
            return
        self.call_from_thread(self.apply_snapshot, snapshot)

    def apply_snapshot(self, snapshot: dict[str, Any], *, width: int | None = None) -> None:
        """Rebuild the runs table from the task tree, preserving the cursor where possible."""
        self._snapshot = snapshot
        table_width = width if width is not None else self.size.width
        self._sync_runs_table_columns(table_width)
        table = self.query_one("#runs", DataTable)
        selected = self._selected_row_key(table)
        self._marked_row = None
        table.clear()
        visible = visible_columns(table_width)
        mode = resolve_mode(interactive=True)
        tree_rows = task_tree_rows(snapshot, self._expanded, table_width, mode=mode)
        keys = [
            tree_row.key.removeprefix("run/") if tree_row.kind == "run" else tree_row.key
            for tree_row in tree_rows
        ]
        if keys:
            if selected is None:
                target = 0
            elif selected in keys:
                target = keys.index(selected)
            else:
                target = len(keys) - 1
        else:
            target = None
        for key, tree_row in zip(keys, tree_rows):
            row = []
            for column in visible:
                value = tree_row.cells[column]
                if column == "status" and tree_row.kind == "run":
                    value = status_badge(str(value), mode)
                elif column_align(column) == "right" and not isinstance(value, Text):
                    value = Text(value, justify="right")
                row.append(value)
            table.add_row(*row, key=key)
        if target is not None:
            table.move_cursor(row=target)
            self._sync_selected_marker(table, target)
        self.title = title_line(snapshot, table_width, mode)
        self.query_one("#stats", Static).update(header_line(snapshot, mode))
        self._sync_panel_chrome(snapshot, mode)
        self._refresh_limits_widget()
        self._refresh_detail(width=table_width)

    @work(thread=True, exclusive=True, group="limits")
    def refresh_limits(self) -> None:
        """Fetch limits off the event loop and apply them; keep the last good ones on failure."""
        if self.limits_source is None:
            return
        try:
            limits = self.limits_source()
        except Exception as exc:  # noqa: BLE001 - any failure must leave the view alive
            self.log(f"limits refresh failed: {exc!r}")
            return
        self.call_from_thread(self.apply_limits, limits)

    def apply_limits(self, limits: dict[str, Any]) -> None:
        """Merge probed limits into the displayed snapshot limits."""
        snapshot_limits = (self._snapshot or {}).get("limits") or {}
        prior = self._probed_limits or {}
        merged = dict(prior)
        for harness, sample in limits.items():
            if sample.get("available"):
                merged[harness] = sample
                continue
            previous = prior.get(harness) or snapshot_limits.get(harness)
            if previous and previous.get("available"):
                continue
            merged[harness] = sample
        self._probed_limits = merged
        self._refresh_limits_widget()

    def _sync_panel_chrome(self, snapshot: dict[str, Any], mode: str) -> None:
        for panel_id in ("stats", "limits", "runs", "detail"):
            widget = self.query_one(f"#{panel_id}")
            widget.border_title = panel_border_title(panel_id, mode)
            widget.border_subtitle = panel_border_subtitle(panel_id, snapshot, mode=mode)

    def _refresh_limits_widget(self) -> None:
        limits_widget = self.query_one("#limits", Static)
        snapshot_limits = (self._snapshot or {}).get("limits") or {}
        if not snapshot_limits:
            limits_widget.display = False
            return
        effective = dict(snapshot_limits)
        if self._probed_limits:
            effective.update(self._probed_limits)
        lines = limits_lines({"limits": effective})
        if not lines:
            limits_widget.display = False
            return
        limits_widget.display = True
        limits_widget.update("\n".join(lines))

    def _show_failure(self, exc_type: str) -> None:
        mode = resolve_mode(interactive=True)
        text = header_line(self._snapshot, mode) if self._snapshot is not None else ""
        self.query_one("#stats", Static).update(f"{text}  |  refresh failed: {exc_type}".strip(" |"))

    def _sync_runs_table_columns(self, width: int | None = None) -> bool:
        """Align DataTable#runs columns with the current terminal width tier."""
        table = self.query_one("#runs", DataTable)
        wanted = list(visible_columns(width if width is not None else self.size.width))
        current = [column.key.value for column in table.ordered_columns]
        if current == wanted:
            return False
        table.clear(columns=True)
        for column in wanted:
            label: str | Text = column
            if column_align(column) == "right":
                label = Text(column, justify="right")
            table.add_column(label, key=column)
        return True

    @staticmethod
    def _selected_run_id(table: DataTable) -> str | None:
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            return str(table.ordered_rows[table.cursor_row].key.value)
        except IndexError:
            return None

    def _selected_run(self) -> dict[str, Any] | None:
        run_id = self._selected_run_id(self.query_one("#runs", DataTable))
        if run_id is None or self._snapshot is None:
            return None
        return next((run for run in self._snapshot.get("runs") or [] if run["run_id"] == run_id), None)

    def _selected_row_key(self, table: DataTable) -> str | None:
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            return str(table.ordered_rows[table.cursor_row].key.value)
        except IndexError:
            return None

    def _refresh_detail(self, *, width: int | None = None) -> None:
        """Re-render the detail pane for the row under the cursor; hide when unsupported."""
        detail = self.query_one("#detail", Static)
        if not detail.display:
            return
        table = self.query_one("#runs", DataTable)
        row_key = self._selected_row_key(table)
        snapshot = self._snapshot
        if row_key is None or snapshot is None:
            detail.display = False
            return
        mode = resolve_mode(interactive=True)
        table_width = width if width is not None else self.size.width
        if row_key == "queue":
            detail.update("\n".join(queue_detail(snapshot, mode=mode)))
            detail.border_title = detail_panel_border_title_queue()
            detail.border_subtitle = detail_panel_border_subtitle_queue(snapshot)
            return
        if row_key.startswith("epic/") and not row_key.endswith("/done"):
            epic_id = row_key.removeprefix("epic/")
            if "/" not in epic_id:
                epic = next(
                    (entry for entry in snapshot.get("epics") or []
                     if entry.get("epic_id") == epic_id),
                    None,
                )
                if epic is not None:
                    detail.update("\n".join(epic_detail(epic, mode=mode)))
                    detail.border_title = detail_panel_border_title_epic(epic)
                    detail.border_subtitle = detail_panel_border_subtitle_epic(epic)
                    return
        run = self._selected_run()
        if run is None:
            detail.display = False
            return
        root = snapshot.get("root")
        log_dir = None if root is None else f"{root}/logs/{run['run_id']}"
        detail.update(format_detail(run, table_width, log_dir, mode=mode))
        detail.border_title = detail_panel_border_title(run)
        detail.border_subtitle = detail_panel_border_subtitle(run)

    def action_cursor_down(self) -> None:
        self.query_one("#runs", DataTable).action_cursor_down()
        self._refresh_detail()

    def action_cursor_up(self) -> None:
        self.query_one("#runs", DataTable).action_cursor_up()
        self._refresh_detail()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "runs":
            return
        self._sync_selected_marker(event.data_table, event.cursor_row)

    def _sync_selected_marker(self, table: DataTable, row: int) -> None:
        prev = self._marked_row
        if prev is not None and prev != row and prev < table.row_count:
            coord = Coordinate(prev, 0)
            cell = table.get_cell_at(coord)
            table.update_cell_at(coord, _without_selected_marker(cell))
        if row < table.row_count:
            coord = Coordinate(row, 0)
            cell = table.get_cell_at(coord)
            plain = cell.plain if isinstance(cell, Text) else str(cell)
            if not plain.startswith(_SELECTED_MARKER):
                table.update_cell_at(coord, _with_selected_marker(cell))
        self._marked_row = row

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        """The focused table consumes `enter` as row selection; treat that as the toggle."""
        self.action_toggle_detail()

    def action_toggle_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        if detail.display:
            detail.display = False
            return
        if self._selected_row_key(self.query_one("#runs", DataTable)) is None:
            return
        detail.display = True
        self._refresh_detail()

    def action_refresh(self) -> None:
        """Re-fetch the snapshot and, when configured, probe harness limits."""
        self.refresh_snapshot()
        if self.limits_source is not None:
            self.refresh_limits()
