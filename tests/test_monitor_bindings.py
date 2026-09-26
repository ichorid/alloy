"""Read-only guarantee for the Textual live view (Component 3).

The monitor process must only ever read state, never mutate a run. This
checks `MonitorApp.BINDINGS` against the allowlist from this bead's Design
section, and greps the source of app.py/render.py for the store-mutating
call names an accidental edit might introduce.
"""

from __future__ import annotations

import inspect

from alloy.monitor import app as app_module
from alloy.monitor import render as render_module
from alloy.monitor.app import MonitorApp
from test_monitor_view import (
    DISABLED_INTERVAL,
    _cursor_row_key,
    _epic,
    _epic_tree_snapshot,
    _ready_bead,
    _run,
    _runs_table,
    _snapshot,
    _table_row_keys,
)

ALLOWED_ACTIONS = {
    "cursor_down",
    "cursor_up",
    "toggle_detail",
    "show_detail",
    "refresh",
    "quit",
    "collapse_tree",
    "expand_tree",
    "toggle_expand_all",
}
FORBIDDEN_TOKENS = (
    "finish_run",
    "update_run",
    "create_run",
    "set_status",
    "set_metadata",
    "claim",
    "cancel(",
    "note(",
)


def _binding_map() -> dict[str, tuple[str, str]]:
    """Map each bound key to (action_name, footer_description)."""
    by_key: dict[str, tuple[str, str]] = {}
    for keys, action, description in MonitorApp.BINDINGS:
        for key in keys.split(","):
            by_key[key.strip()] = (action, description)
    return by_key


def _expandable_keys(snapshot: dict) -> set[str]:
    keys = {f"epic/{epic['epic_id']}" for epic in snapshot.get("epics") or []}
    queue = snapshot.get("queue") or {}
    ready_total = int(queue.get("ready_total") or 0)
    blocked = queue.get("blocked") or []
    if ready_total > 0 or blocked:
        keys.add("queue")
    return keys


def _toggle_all_snapshot() -> dict:
    return _snapshot(
        queue={
            "ready": [_ready_bead("alloy-q.1", epic_id="E")],
            "ready_total": 1,
            "blocked": [],
        },
        epics=[
            _epic("E", title="Epic E", total=2, done=0, running=1),
            _epic("F", title="Epic F", total=1, done=0, running=0),
        ],
        runs=[
            _run("run-under-e", bead_id="bead-under-e", epic_id="E"),
            _run("run-under-f", bead_id="bead-under-f", epic_id="F"),
        ],
    )


def test_every_binding_action_is_in_the_read_only_allowlist():
    actions = {binding[1] for binding in MonitorApp.BINDINGS}

    assert actions <= ALLOWED_ACTIONS


def test_app_and_render_source_contain_no_mutating_store_calls():
    source = inspect.getsource(app_module) + inspect.getsource(render_module)

    for token in FORBIDDEN_TOKENS:
        assert token not in source


def test_tree_navigation_bindings_list_h_l_and_E_with_footer_descriptions():
    by_key = _binding_map()

    assert "h" in by_key
    assert "l" in by_key
    assert "E" in by_key

    h_action, h_description = by_key["h"]
    l_action, l_description = by_key["l"]
    e_action, e_description = by_key["E"]

    assert h_action == "collapse_tree"
    assert l_action == "expand_tree"
    assert e_action == "toggle_expand_all"

    assert h_description.strip()
    assert l_description.strip()
    assert e_description.strip()

    enter_keys = [
        key.strip()
        for keys, action, _description in MonitorApp.BINDINGS
        if action == "toggle_detail"
        for key in keys.split(",")
    ]
    assert "l" not in enter_keys


async def test_l_on_epic_row_expands_its_child_rows():
    snapshot = _epic_tree_snapshot()
    app = MonitorApp(snapshot_source=lambda: snapshot, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = _runs_table(app)

        await pilot.press("j")
        await pilot.pause()
        assert _cursor_row_key(table) == "epic/E"
        assert _table_row_keys(table) == ["epic/E"]

        await pilot.press("l")
        await pilot.pause()

        row_keys = _table_row_keys(table)
        assert "epic/E" in row_keys
        assert "run/run-under-e" in row_keys

        # l is expand-only: a second press must not collapse the epic.
        await pilot.press("l")
        await pilot.pause()
        assert "run/run-under-e" in _table_row_keys(table)
        assert "epic/E" in app._expanded


async def test_h_on_queue_bead_child_collapses_queue_and_moves_cursor_to_queue():
    bead_id = "alloy-q.1"
    snapshot = _snapshot(
        queue={
            "ready": [_ready_bead(bead_id)],
            "ready_total": 1,
            "blocked": [],
        },
    )
    app = MonitorApp(snapshot_source=lambda: snapshot, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = _runs_table(app)

        await pilot.press("l", "j")
        await pilot.pause()
        assert _cursor_row_key(table) == f"queue/{bead_id}"
        assert _table_row_keys(table) == ["queue", f"queue/{bead_id}"]

        await pilot.press("h")
        await pilot.pause()

        assert _table_row_keys(table) == ["queue"]
        assert _cursor_row_key(table) == "queue"
        assert "queue" not in app._expanded


async def test_h_on_epic_queued_child_collapses_epic_and_moves_cursor_to_epic_row():
    bead_id = "queued-under-e"
    snapshot = _snapshot(
        epics=[_epic("E", title="Epic E", total=2, done=0, running=1)],
        queue={
            "ready": [_ready_bead(bead_id, epic_id="E")],
            "ready_total": 1,
            "blocked": [],
        },
        runs=[_run("run-under-e", bead_id="bead-under-e", epic_id="E")],
    )
    app = MonitorApp(snapshot_source=lambda: snapshot, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = _runs_table(app)

        await pilot.press("j", "j", "l", "j", "j")
        await pilot.pause()
        assert _cursor_row_key(table) == f"epic/E/queued/{bead_id}"
        assert f"epic/E/queued/{bead_id}" in _table_row_keys(table)

        await pilot.press("h")
        await pilot.pause()

        assert _table_row_keys(table) == ["queue", "epic/E"]
        assert _cursor_row_key(table) == "epic/E"
        assert "epic/E" not in app._expanded


async def test_h_on_epic_child_collapses_epic_and_moves_cursor_to_epic_row():
    snapshot = _epic_tree_snapshot()
    app = MonitorApp(snapshot_source=lambda: snapshot, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = _runs_table(app)

        await pilot.press("j", "l", "j")
        await pilot.pause()
        assert _cursor_row_key(table) == "run/run-under-e"
        assert "run/run-under-e" in _table_row_keys(table)

        await pilot.press("h")
        await pilot.pause()

        assert _table_row_keys(table) == ["epic/E"]
        assert _cursor_row_key(table) == "epic/E"


async def test_E_press_twice_toggles_all_queue_and_epic_rows_expanded():
    snapshot = _toggle_all_snapshot()
    expandable = _expandable_keys(snapshot)
    assert expandable == {"queue", "epic/E", "epic/F"}

    app = MonitorApp(snapshot_source=lambda: snapshot, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = _runs_table(app)

        collapsed_keys = _table_row_keys(table)
        assert collapsed_keys == ["queue", "epic/E", "epic/F"]
        assert app._expanded == set()

        await pilot.press("E")
        await pilot.pause()

        assert app._expanded == expandable
        expanded_keys = _table_row_keys(table)
        assert "queue/alloy-q.1" in expanded_keys
        assert "run/run-under-e" in expanded_keys
        assert "run/run-under-f" in expanded_keys

        await pilot.press("E")
        await pilot.pause()

        assert app._expanded == set()
        assert _table_row_keys(table) == collapsed_keys


async def test_refresh_keeps_horizontal_scroll_of_runs_table():
    snapshot = _epic_tree_snapshot()
    app = MonitorApp(snapshot_source=lambda: snapshot, interval=DISABLED_INTERVAL)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        table = _runs_table(app)
        assert table.max_scroll_x > 0  # the narrow window overflows horizontally
        table.scroll_to(x=1, animate=False)
        await pilot.pause()
        assert table.scroll_x == 1

        app.apply_snapshot(snapshot)

        # Immediately, not after a later refresh: no frame may show the origin.
        assert table.scroll_x == 1
        await pilot.pause()

        assert table.scroll_x == 1
