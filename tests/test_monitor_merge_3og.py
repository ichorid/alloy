"""Merge-resolution acceptance tests for alloy-3og (main into alloy/alloy-3g0).

Asserts that alloy-gek compact ``_reset_suffix`` semantics and epic-scoped queue
keys from main coexist with alloy-3g0.3 nerd limits styling on the merged tree.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

_AMSTERDAM = ZoneInfo("Europe/Amsterdam")


def _freeze_render_now(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    import alloy.monitor.render as render_mod

    real_datetime = render_mod.datetime

    class _FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return when.astimezone().replace(tzinfo=None)
            return when.astimezone(tz)

    monkeypatch.setattr(render_mod, "datetime", _FrozenDatetime)


def _snapshot(
    *,
    runs=(),
    queue=None,
    epics=(),
) -> dict:
    return {
        "root": "/home/vader/.alloy",
        "repo": "/home/vader/MY_SRC/alloy",
        "scheduler": {"running": False, "pid": None},
        "ready_count": 0,
        "ready_capped_at": 1000,
        "lifetime": {"done": 0, "failed": 0, "cancelled": 0},
        "runs": list(runs),
        "queue": queue if queue is not None else {"ready": [], "ready_total": 0, "blocked": []},
        "epics": list(epics),
    }


def _ready_bead(bead_id: str, *, epic_id: str | None = None) -> dict:
    return {
        "bead_id": bead_id,
        "title": bead_id,
        "recipe": "tdd-loop",
        "priority": 2,
        "complexity": None,
        "epic_id": epic_id,
    }


def _epic(epic_id: str, *, total: int = 1, done: int = 0) -> dict:
    return {
        "epic_id": epic_id,
        "title": epic_id,
        "total": total,
        "done": done,
        "done_ids": [],
        "running": 0,
        "judge": 0,
    }


def _limits_line_for_single_window(
    label: str,
    resets_at: str,
    *,
    harness: str = "claude",
    used_percent: float = 42.0,
    key: str = "five_hour",
    mode: str | None = None,
) -> str:
    from alloy.limits import window
    from alloy.monitor.render import limits_lines

    snapshot = _snapshot()
    snapshot["limits"] = {
        harness: {
            "harness": harness,
            "installed": True,
            "available": True,
            "fetched_at": "2026-09-24T08:00:00+00:00",
            "as_of": "2026-09-24T08:00:00+00:00",
            "source": "oauth-usage-api",
            "error": None,
            "status": None,
            "windows": [window(key, label, used_percent, resets_at)],
        },
    }
    kwargs = {"mode": mode} if mode is not None else {}
    lines = limits_lines(snapshot, **kwargs)
    assert len(lines) == 1
    return lines[0]


def test_reset_suffix_exported_from_render_module():
    from alloy.monitor.render import _reset_suffix

    assert _reset_suffix("5h", "2026-09-24T14:00:00+00:00") is not None


def test_resets_hhmm_removed_after_main_merge():
    import alloy.monitor.render as render_mod

    assert not hasattr(render_mod, "_resets_hhmm")


def test_ascii_limits_use_compact_reset_suffix_without_resets_word(
    monkeypatch: pytest.MonkeyPatch,
):
    _freeze_render_now(
        monkeypatch,
        datetime(2026, 9, 24, 10, 0, tzinfo=_AMSTERDAM),
    )
    line = _limits_line_for_single_window(
        "5h",
        "2026-09-24T14:00:00+00:00",
        mode="ascii",
    )

    assert "resets" not in line
    assert "(16:00)" in line


def test_nerd_limits_clock_icon_uses_local_reset_suffix_not_utc_hhmm(
    monkeypatch: pytest.MonkeyPatch,
):
    _freeze_render_now(
        monkeypatch,
        datetime(2026, 9, 24, 10, 0, tzinfo=_AMSTERDAM),
    )
    line = _limits_line_for_single_window(
        "5h",
        "2026-09-24T18:00:00+00:00",
        mode="nerd",
    )

    assert "\uf017" in line
    assert "20:00" in line
    assert "18:00" not in line
    assert "(resets" not in line
    assert "(20:00)" not in line


def test_nerd_limits_weekly_reset_uses_local_date_suffix_with_calendar_icon(
    monkeypatch: pytest.MonkeyPatch,
):
    _freeze_render_now(
        monkeypatch,
        datetime(2026, 9, 24, 10, 0, tzinfo=_AMSTERDAM),
    )
    line = _limits_line_for_single_window(
        "weekly",
        "2026-09-24T23:00:00+00:00",
        key="seven_day",
        mode="nerd",
    )

    assert "\uf073" in line
    assert "Sep 25" in line
    assert "\uf017" not in line
    assert "resets" not in line
    assert "2026" not in line


def test_task_tree_epic_scoped_queued_row_uses_epic_queued_key():
    from alloy.monitor.render import task_tree_rows

    snap = _snapshot(
        queue={
            "ready": [_ready_bead("alloy-x", epic_id="E")],
            "ready_total": 1,
            "blocked": [],
        },
        epics=[_epic("E", total=2, done=0)],
        runs=[],
    )
    rows = task_tree_rows(snap, {"epic/E"}, width=100, mode="ascii")
    keys = {row.key for row in rows}

    assert "epic/E/queued/alloy-x" in keys
    assert "queue/alloy-x" not in keys
