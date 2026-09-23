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
from textual.events import Resize
from textual.widgets import DataTable, Footer, Header, Static

from alloy.monitor.render import (
    COLUMNS,
    format_detail,
    header_line,
    limits_lines,
    run_rows,
    status_color,
    visible_columns,
)


class MonitorApp(App[None]):
    """Live dashboard over a `snapshot_source` callable returning the frozen snapshot dict."""

    TITLE = "alloy monitor"
    CSS_PATH = "monitor.tcss"
    BINDINGS = [
        ("j,down", "cursor_down", "move"),
        ("k,up", "cursor_up", ""),
        ("enter,l", "toggle_detail", "Detail"),
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
        yield Footer()

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
        """Rebuild the runs table keyed by run_id, preserving the cursor where possible."""
        self._snapshot = snapshot
        table_width = width if width is not None else self.size.width
        self._sync_runs_table_columns(table_width)
        table = self.query_one("#runs", DataTable)
        selected = self._selected_run_id(table)
        table.clear()
        run_ids = [run["run_id"] for run in snapshot.get("runs") or []]
        visible = visible_columns(table_width)
        for run_id, cells in zip(run_ids, run_rows(snapshot)):
            by_column = dict(zip(COLUMNS, cells))
            row = []
            for column in visible:
                value = by_column[column]
                if column == "status":
                    value = Text(value, style=status_color(value))
                row.append(value)
            table.add_row(*row, key=run_id)
        if run_ids:
            if selected is None:
                target = 0
            elif selected in run_ids:
                target = run_ids.index(selected)
            else:
                target = len(run_ids) - 1
            table.move_cursor(row=target)
        self.query_one("#stats", Static).update(header_line(snapshot))
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
        self._probed_limits = limits
        self._refresh_limits_widget()

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
        text = header_line(self._snapshot) if self._snapshot is not None else ""
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
            table.add_column(column, key=column)
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

    def _refresh_detail(self, *, width: int | None = None) -> None:
        """Re-render the detail pane for the run under the cursor; hide it when there is none."""
        detail = self.query_one("#detail", Static)
        if not detail.display:
            return
        run = self._selected_run()
        if run is None:
            detail.display = False
            return
        root = (self._snapshot or {}).get("root")
        log_dir = None if root is None else f"{root}/logs/{run['run_id']}"
        detail.update(format_detail(run, width if width is not None else self.size.width, log_dir))

    def action_cursor_down(self) -> None:
        self.query_one("#runs", DataTable).action_cursor_down()
        self._refresh_detail()

    def action_cursor_up(self) -> None:
        self.query_one("#runs", DataTable).action_cursor_up()
        self._refresh_detail()

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        """The focused table consumes `enter` as row selection; treat that as the toggle."""
        self.action_toggle_detail()

    def action_toggle_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        if detail.display:
            detail.display = False
            return
        if self._selected_run() is None:
            return
        detail.display = True
        self._refresh_detail()
