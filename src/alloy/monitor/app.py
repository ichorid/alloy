"""Textual live view for `alloy monitor`: stats line, runs table, detail pane, refresh, quit.

Strictly read-only: the app only ever calls `snapshot_source()` (off the event
loop, in a thread worker) and touches its own widgets. See "Component 3" in
docs/plans/execution-monitor.md.
"""

from __future__ import annotations

import threading

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
    activity_line,
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
    queued_detail,
    status_badge,
    task_tree_rows,
    title_line,
    visible_columns,
)

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
        ("enter", "toggle_detail", "Detail"),
        ("e", "show_detail", "Detail"),
        ("h", "collapse_tree", "Collapse"),
        ("l", "expand_tree", "Expand"),
        ("E", "toggle_expand_all", "All"),
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        snapshot_source: Callable[[], dict[str, Any]],
        interval: float = 1.0,
        limits_source: Callable[[], dict[str, Any]] | None = None,
        limits_interval: float = 60.0,
        limits_style: str = "remaining",
    ) -> None:
        super().__init__()
        self.snapshot_source = snapshot_source
        self.interval = interval
        self.limits_source = limits_source
        self.limits_interval = limits_interval
        if limits_style not in ("remaining", "spent"):
            raise ValueError("limits_style must be 'remaining' or 'spent'")
        self.limits_style = limits_style
        self._refresh_lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._probed_limits: dict[str, Any] | None = None
        self._marked_row: int | None = None
        self._expanded: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        stats = Static("loading…", id="stats")
        stats.border_title = "STATS"
        yield stats
        activity = Static("active model | action: none: idle", id="activity")
        activity.border_title = "NOW"
        yield activity
        limits = Static("", id="limits", markup=True)
        limits.border_title = f"LIMITS ({self.limits_style})"
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

    @work(thread=True)
    def refresh_snapshot(self) -> None:
        """Fetch a snapshot off the event loop and apply it; keep the last good one on failure.

        At most one fetch is in flight: a thread blocked in a `bd` subprocess cannot be
        cancelled, so `exclusive=True` let slow snapshots pile up and starve each other.
        A tick that finds one running is simply skipped.
        """
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            snapshot = self.snapshot_source()
        except Exception as exc:  # noqa: BLE001 - any failure must leave the view alive
            self.log(f"refresh failed: {exc!r}")
            self.call_from_thread(self._show_failure, type(exc).__name__)
            return
        finally:
            self._refresh_lock.release()
        self.call_from_thread(self.apply_snapshot, snapshot)

    def apply_snapshot(self, snapshot: dict[str, Any], *, width: int | None = None) -> None:
        """Rebuild the runs table from the task tree, preserving cursor and expansion."""
        self._snapshot = snapshot
        table_width = width if width is not None else self.size.width
        self._sync_runs_table_columns(table_width)
        table = self.query_one("#runs", DataTable)
        selected_key = self._selected_row_key(table)
        scroll_x, scroll_y = table.scroll_x, table.scroll_y
        self._marked_row = None
        table.clear()
        visible = visible_columns(table_width)
        mode = resolve_mode(interactive=True)
        tree_rows = task_tree_rows(snapshot, self._expanded, table_width, mode=mode)
        row_keys: list[str] = []
        seen_keys: set[str] = set()
        target: int | None = None
        for tree_row in tree_rows:
            if tree_row.key in seen_keys:
                continue
            seen_keys.add(tree_row.key)
            row_keys.append(tree_row.key)
            row = []
            for column in visible:
                value = tree_row.cells.get(column, "-")
                if column == "status" and tree_row.kind == "run":
                    value = status_badge(str(value), mode)
                elif column_align(column) == "right" and not isinstance(value, Text):
                    value = Text(value, justify="right")
                row.append(value)
            table.add_row(*row, key=tree_row.key)
        if row_keys:
            if selected_key is None:
                target = 0
            elif selected_key in row_keys:
                target = row_keys.index(selected_key)
            else:
                target = len(row_keys) - 1
        if target is not None:
            table.move_cursor(row=target, scroll=False)
            self._sync_selected_marker(table, target)
        # clear() resets the viewport; a periodic refresh must not throw away where
        # the user scrolled. Restore it now, before the next paint (the virtual size
        # is only recomputed on idle, so the old scroll range still accepts it) --
        # restoring only after a refresh shows one frame at the origin (flicker).
        # The deferred call covers the case where the size did change.
        table.scroll_x = table.scroll_target_x = scroll_x
        table.scroll_y = table.scroll_target_y = scroll_y
        self.call_after_refresh(table.scroll_to, scroll_x, scroll_y, animate=False)
        self.title = title_line(snapshot, table_width, mode)
        self.query_one("#stats", Static).update(header_line(snapshot, mode))
        self.query_one("#activity", Static).update(activity_line(snapshot))
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
        mode = resolve_mode(interactive=True)
        lines = limits_lines(
            {"limits": effective}, mode=mode, width=self.size.width,
            usage_style=self.limits_style,
        )
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

    def _selected_row_key(self, table: DataTable) -> str | None:
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            return str(table.ordered_rows[table.cursor_row].key.value)
        except IndexError:
            return None

    @staticmethod
    def _is_expandable_row_key(row_key: str) -> bool:
        if row_key == "queue":
            return True
        if not row_key.startswith("epic/"):
            return False
        return "/" not in row_key.removeprefix("epic/")

    def _toggle_expansion(self, row_key: str) -> None:
        if not self._is_expandable_row_key(row_key):
            return
        if row_key in self._expanded:
            self._expanded.discard(row_key)
        else:
            self._expanded.add(row_key)
        if self._snapshot is not None:
            self.apply_snapshot(self._snapshot)

    def _expandable_keys(self, snapshot: dict[str, Any]) -> set[str]:
        keys = {f"epic/{epic['epic_id']}" for epic in snapshot.get("epics") or []}
        queue = snapshot.get("queue") or {}
        ready_total = int(queue.get("ready_total") or 0)
        blocked = queue.get("blocked") or []
        if ready_total > 0 or blocked or "queue" in self._expanded:
            keys.add("queue")
        return keys

    def _parent_expandable_key(self, row_key: str) -> str | None:
        if row_key.startswith("run/"):
            if self._snapshot is None:
                return None
            run_id = row_key.removeprefix("run/")
            run = next(
                (entry for entry in self._snapshot.get("runs") or []
                 if entry["run_id"] == run_id),
                None,
            )
            if run is None:
                return None
            epic_id = run.get("epic_id")
            return f"epic/{epic_id}" if epic_id else None
        if row_key.startswith("queue/"):
            return "queue"
        if row_key.startswith("epic/") and "/queued/" in row_key:
            epic_id = row_key.split("/")[1]
            return f"epic/{epic_id}"
        if row_key.startswith("epic/") and row_key.endswith("/done"):
            return row_key.removesuffix("/done")
        return None

    def _move_cursor_to_key(self, row_key: str) -> None:
        table = self.query_one("#runs", DataTable)
        row_keys = [str(entry.key.value) for entry in table.ordered_rows]
        if row_key in row_keys:
            table.move_cursor(row=row_keys.index(row_key))
            self._sync_selected_marker(table, row_keys.index(row_key))

    def action_collapse_tree(self) -> None:
        row_key = self._selected_row_key(self.query_one("#runs", DataTable))
        if row_key is None or self._snapshot is None:
            return
        if self._is_expandable_row_key(row_key) and row_key in self._expanded:
            self._expanded.discard(row_key)
            self.apply_snapshot(self._snapshot)
            self._refresh_detail()
            return
        parent = self._parent_expandable_key(row_key)
        if parent is None or parent not in self._expanded:
            return
        self._expanded.discard(parent)
        self._move_cursor_to_key(parent)
        self.apply_snapshot(self._snapshot)
        self._refresh_detail()

    def action_expand_tree(self) -> None:
        row_key = self._selected_row_key(self.query_one("#runs", DataTable))
        if row_key is None or self._snapshot is None:
            return
        if not self._is_expandable_row_key(row_key) or row_key in self._expanded:
            return
        self._expanded.add(row_key)
        self.apply_snapshot(self._snapshot)
        self._refresh_detail()

    def action_toggle_expand_all(self) -> None:
        if self._snapshot is None:
            return
        expandable = self._expandable_keys(self._snapshot)
        if expandable and expandable <= self._expanded:
            self._expanded.clear()
        else:
            self._expanded.update(expandable)
        self.apply_snapshot(self._snapshot)
        self._refresh_detail()

    def _selected_run(self) -> dict[str, Any] | None:
        row_key = self._selected_row_key(self.query_one("#runs", DataTable))
        if row_key is None or self._snapshot is None or not row_key.startswith("run/"):
            return None
        run_id = row_key.removeprefix("run/")
        return next(
            (run for run in self._snapshot.get("runs") or [] if run["run_id"] == run_id),
            None,
        )

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
        if row_key.startswith("queue/") or "/queued/" in row_key:
            bead_id = row_key.rsplit("/", 1)[-1]
            queue = snapshot.get("queue") or {}
            bead = next(
                (b for b in (queue.get("ready") or []) + (queue.get("blocked") or [])
                 if b.get("bead_id") == bead_id),
                None,
            )
            if bead is None:
                detail.display = False
                return
            detail.update("\n".join(queued_detail(bead, mode=mode)))
            detail.border_title = bead_id
            detail.border_subtitle = "blocked" if bead.get("blocked_by") else "ready"
            return
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
        detail.update(format_detail(
            run, table_width, log_dir, mode=mode, usage_style=self.limits_style
        ))
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
        """No row marker: the cursor row is already highlighted bold."""
        self._marked_row = row

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        """Enter on queue/epic toggles expansion; on a run row toggles the detail pane."""
        row_key = self._selected_row_key(self.query_one("#runs", DataTable))
        if row_key is not None and self._is_expandable_row_key(row_key):
            self._toggle_expansion(row_key)
            return
        self.action_toggle_detail()

    def action_toggle_detail(self) -> None:
        row_key = self._selected_row_key(self.query_one("#runs", DataTable))
        if row_key is not None and self._is_expandable_row_key(row_key):
            self._toggle_expansion(row_key)
            return
        self.action_show_detail()

    def action_show_detail(self) -> None:
        """Toggle the detail pane for any row (epics, queue, queued beads, runs)."""
        detail = self.query_one("#detail", Static)
        if detail.display:
            detail.display = False
            return
        detail.display = True
        self._refresh_detail()

    def action_refresh(self) -> None:
        """Re-fetch the snapshot and, when configured, probe harness limits."""
        self.refresh_snapshot()
        if self.limits_source is not None:
            self.refresh_limits()
