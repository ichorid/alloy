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

ALLOWED_ACTIONS = {"cursor_down", "cursor_up", "toggle_detail", "refresh", "quit"}
FORBIDDEN_TOKENS = (
    "finish_run", "update_run", "create_run", "set_status", "set_metadata",
    "claim", "cancel(", "note(",
)


def test_every_binding_action_is_in_the_read_only_allowlist():
    actions = {binding[1] for binding in MonitorApp.BINDINGS}

    assert actions <= ALLOWED_ACTIONS


def test_app_and_render_source_contain_no_mutating_store_calls():
    source = inspect.getsource(app_module) + inspect.getsource(render_module)

    for token in FORBIDDEN_TOKENS:
        assert token not in source
