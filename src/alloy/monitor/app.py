"""Textual live view for `alloy monitor`: stats line, runs table, refresh, quit.

Strictly read-only: the app only ever calls `snapshot_source()` (off the event
loop, in a thread worker) and touches its own widgets. See "Component 3" in
docs/plans/execution-monitor.md.
"""

from __future__ import annotations

from typing import Any, Callable

from textual import work
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, Static

from alloy.monitor.render import COLUMNS, header_line, run_rows


class MonitorApp(App[None]):
    """Live dashboard over a `snapshot_source` callable returning the frozen snapshot dict."""

    TITLE = "alloy monitor"
    BINDINGS = [
        ("j,down", "cursor_down", "Down"),
        ("k,up", "cursor_up", "Up"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, snapshot_source: Callable[[], dict[str, Any]], interval: float = 1.0) -> None:
        super().__init__()
        self.snapshot_source = snapshot_source
        self.interval = interval
        self._snapshot: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("loading…", id="stats")
        yield DataTable(id="runs", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#runs", DataTable)
        for column in COLUMNS:
            table.add_column(column, key=column)
        self.refresh_snapshot()
        self.set_interval(self.interval, self.refresh_snapshot)

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

    def apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Rebuild the runs table keyed by run_id, preserving the cursor where possible."""
        self._snapshot = snapshot
        table = self.query_one("#runs", DataTable)
        selected = self._selected_run_id(table)
        table.clear()
        run_ids = [run["run_id"] for run in snapshot.get("runs") or []]
        for run_id, cells in zip(run_ids, run_rows(snapshot)):
            table.add_row(*cells, key=run_id)
        if run_ids:
            if selected is None:
                target = 0
            elif selected in run_ids:
                target = run_ids.index(selected)
            else:
                target = len(run_ids) - 1
            table.move_cursor(row=target)
        self.query_one("#stats", Static).update(header_line(snapshot))

    def _show_failure(self, exc_type: str) -> None:
        text = header_line(self._snapshot) if self._snapshot is not None else ""
        self.query_one("#stats", Static).update(f"{text}  |  refresh failed: {exc_type}".strip(" |"))

    @staticmethod
    def _selected_run_id(table: DataTable) -> str | None:
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            return str(table.ordered_rows[table.cursor_row].key.value)
        except IndexError:
            return None

    def action_cursor_down(self) -> None:
        self.query_one("#runs", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#runs", DataTable).action_cursor_up()
