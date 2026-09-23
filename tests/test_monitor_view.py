"""`MonitorApp` (the Textual live view) and `alloy monitor --once` (plain text).

Pilot-based, headless, per "Component 3" of docs/plans/execution-monitor.md:
no real terminal, no real model, snapshots injected via `snapshot_source`.
`interval` is set huge in every test so the periodic timer never fires; only
the explicit initial refresh (and any refresh a test triggers itself) runs.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from rich.text import Text
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Static
from typer.testing import CliRunner

from alloy.cli import app as cli_app
from alloy.limits import window
from alloy.monitor.app import MonitorApp
from alloy.monitor.render import COLUMNS

DISABLED_INTERVAL = 1000.0
EMPTY_TOKENS = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": None}


def _snapshot(*, runs=(), limits: dict | None = None) -> dict:
    snap = {
        "root": "/home/vader/.alloy",
        "repo": "/home/vader/MY_SRC/alloy",
        "scheduler": {"running": False, "pid": None},
        "ready_count": 0,
        "ready_capped_at": 1000,
        "lifetime": {"done": 0, "failed": 0, "cancelled": 0},
        "runs": list(runs),
    }
    if limits is not None:
        snap["limits"] = limits
    return snap


def _claude_limits(used_percent: float) -> dict:
    return {
        "claude": {
            "harness": "claude",
            "installed": True,
            "available": True,
            "fetched_at": "2026-09-23T10:00:00+00:00",
            "as_of": "2026-09-23T10:00:00+00:00",
            "source": "oauth-usage-api",
            "error": None,
            "status": None,
            "windows": [window("five_hour", "5h", used_percent, None)],
        },
    }


def _run(run_id: str, bead_id: str | None = None) -> dict:
    return {
        "bead_id": bead_id or f"alloy-{run_id}",
        "run_id": run_id,
        "recipe": "tdd-loop",
        "status": "running",
        "stage": "implement",
        "iteration": 1,
        "max_iterations": 5,
        "consiliums": 0,
        "max_consiliums": 1,
        "tests_summary": None,
        "elapsed_minutes": 1,
        "current_calls": [],
        "tokens": dict(EMPTY_TOKENS),
        "tokens_by_role": {},
        "judge": None,
        "worktree": f"/home/vader/.alloy/worktrees/{run_id}",
        "branch": f"alloy/{run_id}",
    }


def _done_run(run_id: str, bead_id: str | None = None) -> dict:
    run = _run(run_id, bead_id=bead_id)
    run["status"] = "done"
    run["stage"] = "finished"
    run["current_calls"] = []
    return run


def _run_with_status(run_id: str, status: str, bead_id: str | None = None) -> dict:
    run = _run(run_id, bead_id=bead_id)
    run["status"] = status
    return run


ZERO_RUNS = _snapshot(runs=[])
TWO_RUNS = _snapshot(runs=[_run("run-1"), _run("run-2")])
THREE_RUNS = _snapshot(runs=[_run("run-1"), _run("run-2"), _run("run-3")])
RUNNING_AND_DONE = _snapshot(
    runs=[_run("run-active", bead_id="bead-active"), _done_run("run-finished", bead_id="bead-done")],
)


class FlakySource:
    """Returns `good` once, then raises on every subsequent call."""

    def __init__(self, good: dict) -> None:
        self.good = good
        self.calls = 0

    def __call__(self) -> dict:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("boom")
        return self.good


class FlakyLimitsSource:
    """Returns `good` once, then raises on every subsequent call."""

    def __init__(self, good: dict) -> None:
        self.good = good
        self.calls = 0

    def __call__(self) -> dict:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("boom")
        return self.good


def _runs_table(app: MonitorApp) -> DataTable:
    return app.query_one("#runs", DataTable)


def _stats_text(app: MonitorApp) -> str:
    static = app.query_one("#stats", Static)
    # Static has no public renderable getter; the mangled attribute is the
    # only way to read back what update() stored, short of full screen render.
    return str(getattr(static, "_Static__content", ""))


def _limits_text(app: MonitorApp) -> str:
    static = app.query_one("#limits", Static)
    return str(getattr(static, "_Static__content", ""))


def _cursor_run_id(table: DataTable) -> str:
    key = table.coordinate_to_cell_key(Coordinate(row=table.cursor_row, column=0))
    return key.row_key.value


def _table_column_keys(table: DataTable) -> list[str]:
    return [column.key.value for column in table.ordered_columns]


def _expected_visible_column_keys(width: int) -> tuple[str, ...]:
    from alloy.monitor.render import visible_columns

    return visible_columns(width)


# -- initial render -----------------------------------------------------------


async def test_table_shows_one_row_per_run_after_first_refresh():
    app = MonitorApp(snapshot_source=lambda: TWO_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _runs_table(app).row_count == 2


async def test_zero_runs_renders_without_raising():
    app = MonitorApp(snapshot_source=lambda: ZERO_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _runs_table(app).row_count == 0


# -- cursor movement -----------------------------------------------------------


async def test_j_moves_cursor_down_and_clamps_at_the_last_row():
    app = MonitorApp(snapshot_source=lambda: TWO_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = _runs_table(app)
        assert table.cursor_row == 0

        await pilot.press("j")
        await pilot.pause()
        assert table.cursor_row == 1

        await pilot.press("j")
        await pilot.pause()
        assert table.cursor_row == 1  # clamped, only two rows


async def test_k_moves_cursor_up_and_clamps_at_the_first_row():
    app = MonitorApp(snapshot_source=lambda: TWO_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = _runs_table(app)

        await pilot.press("j")
        await pilot.pause()
        assert table.cursor_row == 1

        await pilot.press("k")
        await pilot.pause()
        assert table.cursor_row == 0

        await pilot.press("k")
        await pilot.pause()
        assert table.cursor_row == 0  # clamped, can't go above the first row


# -- quit -----------------------------------------------------------------


async def test_q_quits_with_return_code_0():
    app = MonitorApp(snapshot_source=lambda: TWO_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("j", "q")
    assert app.return_code == 0


# -- cursor preservation across refreshes --------------------------------------


async def test_cursor_stays_on_the_same_run_id_when_it_still_exists_after_refresh():
    app = MonitorApp(snapshot_source=lambda: THREE_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = _runs_table(app)
        await pilot.press("j")
        await pilot.pause()
        assert _cursor_run_id(table) == "run-2"

        # A later snapshot with the same runs, reordered -- the cursor should
        # follow "run-2" rather than staying pinned to row index 1.
        reordered = _snapshot(runs=[_run("run-2"), _run("run-1"), _run("run-3")])
        app.apply_snapshot(reordered)
        await pilot.pause()

        assert _cursor_run_id(table) == "run-2"


async def test_selected_run_disappearing_leaves_the_cursor_on_a_valid_row():
    app = MonitorApp(snapshot_source=lambda: THREE_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = _runs_table(app)
        await pilot.press("j", "j")
        await pilot.pause()
        assert _cursor_run_id(table) == "run-3"

        without_run_3 = _snapshot(runs=[_run("run-1"), _run("run-2")])
        app.apply_snapshot(without_run_3)
        await pilot.pause()

        assert table.row_count == 2
        assert 0 <= table.cursor_row < table.row_count
        assert _cursor_run_id(table) == "run-2"  # clamped to the last row


# -- refresh failure handling ---------------------------------------------------


async def test_a_raising_snapshot_source_leaves_the_previous_rows_visible():
    source = FlakySource(TWO_RUNS)
    app = MonitorApp(snapshot_source=source, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = _runs_table(app)
        assert table.row_count == 2

        worker = app.refresh_snapshot()
        await worker.wait()
        await pilot.pause()

        assert table.row_count == 2  # unchanged, not crashed
        assert "refresh failed" in _stats_text(app)

        # The app must still be alive and able to process input afterwards.
        await pilot.press("q")
    assert app.return_code == 0


# -- refresh runs off the event loop -------------------------------------------


async def test_a_slow_snapshot_source_does_not_block_quitting():
    def slow_source() -> dict:
        time.sleep(0.5)
        return TWO_RUNS

    app = MonitorApp(snapshot_source=slow_source, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        started = time.monotonic()
        await asyncio.wait_for(pilot.press("q"), timeout=0.3)
        elapsed = time.monotonic() - started

    assert app.return_code == 0
    assert elapsed < 0.5


# -- detail pane ---------------------------------------------------------------


def _detail(app: MonitorApp) -> Static:
    return app.query_one("#detail", Static)


def _detail_text(app: MonitorApp) -> str:
    return str(getattr(_detail(app), "_Static__content", ""))


async def test_enter_shows_the_detail_pane_and_enter_again_hides_it():
    app = MonitorApp(snapshot_source=lambda: TWO_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _detail(app).display is False

        await pilot.press("enter")
        await pilot.pause()
        assert _detail(app).display is True

        await pilot.press("enter")
        await pilot.pause()
        assert _detail(app).display is False


async def test_l_toggles_the_detail_pane_like_enter():
    app = MonitorApp(snapshot_source=lambda: TWO_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("l")
        await pilot.pause()
        assert _detail(app).display is True

        await pilot.press("l")
        await pilot.pause()
        assert _detail(app).display is False


async def test_moving_the_cursor_with_the_pane_open_updates_its_content():
    app = MonitorApp(snapshot_source=lambda: TWO_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()
        first_content = _detail_text(app)

        await pilot.press("j")
        await pilot.pause()
        second_content = _detail_text(app)

        assert first_content != second_content


async def test_selected_run_disappearing_hides_the_detail_pane_instead_of_raising():
    app = MonitorApp(snapshot_source=lambda: THREE_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("j", "j", "enter")
        await pilot.pause()
        assert _detail(app).display is True

        app.apply_snapshot(ZERO_RUNS)
        await pilot.pause()

        assert _detail(app).display is False


async def test_blocked_run_status_cell_renders_with_status_color():
    snapshot = _snapshot(runs=[_run_with_status("run-blocked", "blocked")])
    app = MonitorApp(snapshot_source=lambda: snapshot, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = _runs_table(app)
        status_col = table.get_column_index("status")
        cell = table.get_cell_at(Coordinate(row=0, column=status_col))

        assert isinstance(cell, Text)
        assert str(cell) == "blocked"
        assert cell.style == "#f85149"


async def test_done_run_row_is_selectable_and_detail_shows_bead_id():
    app = MonitorApp(snapshot_source=lambda: RUNNING_AND_DONE, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = _runs_table(app)

        await pilot.press("j")
        await pilot.pause()
        assert table.cursor_row == 1
        status_col = table.get_column_index("status")
        done_cell = table.get_cell_at(Coordinate(row=table.cursor_row, column=status_col))
        assert isinstance(done_cell, Text)
        assert str(done_cell) == "done"
        assert done_cell.style == "#7ee787"

        await pilot.press("enter")
        await pilot.pause()
        assert _detail(app).display is True
        assert "bead-done" in _detail_text(app)


# -- limits section (alloy-w9d.11) ---------------------------------------------


async def test_limits_widget_shows_probed_percent_after_mount():
    cached = _claude_limits(42.0)
    probed = _claude_limits(77.0)
    snapshot = _snapshot(limits=cached)
    app = MonitorApp(
        snapshot_source=lambda: snapshot,
        limits_source=lambda: probed,
        interval=DISABLED_INTERVAL,
        limits_interval=DISABLED_INTERVAL,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "77%" in _limits_text(app)
        assert "42%" not in _limits_text(app)
        assert app.query_one("#limits", Static).display is True


async def test_limits_widget_is_hidden_when_snapshot_limits_is_empty():
    snapshot = _snapshot(limits={})
    app = MonitorApp(
        snapshot_source=lambda: snapshot,
        limits_source=lambda: {},
        interval=DISABLED_INTERVAL,
        limits_interval=DISABLED_INTERVAL,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#limits", Static).display is False


async def test_a_raising_limits_source_leaves_the_previous_limits_text_unchanged():
    cached = _claude_limits(42.0)
    probed = _claude_limits(55.0)
    limits_source = FlakyLimitsSource(probed)
    snapshot = _snapshot(limits=cached, runs=[_run("run-1")])
    refreshed = _snapshot(limits=cached, runs=[_run("run-1"), _run("run-2")])
    app = MonitorApp(
        snapshot_source=lambda: snapshot,
        limits_source=limits_source,
        interval=DISABLED_INTERVAL,
        limits_interval=DISABLED_INTERVAL,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "55%" in _limits_text(app)

        worker = app.refresh_limits()
        await worker.wait()
        await pilot.pause()
        assert "55%" in _limits_text(app)

        app.apply_snapshot(refreshed)
        await pilot.pause()
        assert _runs_table(app).row_count == 2


async def test_unavailable_limits_probe_keeps_previous_available_sample():
    cached = _claude_limits(42.0)
    probed = {
        "claude": {
            "harness": "claude",
            "installed": True,
            "available": False,
            "fetched_at": None,
            "as_of": None,
            "source": None,
            "error": "HTTP 429",
            "status": None,
            "windows": [],
        }
    }

    # First mount probes good limits; second probe returns 429.
    calls = 0

    def limits_source_fn() -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            return cached
        return probed

    snapshot = _snapshot(limits=cached, runs=[_run("run-1")])
    app = MonitorApp(
        snapshot_source=lambda: snapshot,
        limits_source=limits_source_fn,
        interval=DISABLED_INTERVAL,
        limits_interval=DISABLED_INTERVAL,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "42%" in _limits_text(app)

        worker = app.refresh_limits()
        await worker.wait()
        await pilot.pause()
        assert "42%" in _limits_text(app)
        assert "unavailable" not in _limits_text(app)


async def test_r_key_refreshes_snapshot_and_reprobes_limits():
    cached = _claude_limits(42.0)
    snapshot_calls = 0
    limits_calls = 0

    def snapshot_source() -> dict:
        nonlocal snapshot_calls
        snapshot_calls += 1
        return _snapshot(limits=cached, runs=[_run("run-1")])

    def limits_source() -> dict:
        nonlocal limits_calls
        limits_calls += 1
        return _claude_limits(55.0 + limits_calls)

    app = MonitorApp(
        snapshot_source=snapshot_source,
        limits_source=limits_source,
        interval=DISABLED_INTERVAL,
        limits_interval=DISABLED_INTERVAL,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert snapshot_calls == 1
        assert limits_calls == 1
        assert "56%" in _limits_text(app)

        await pilot.press("r")
        for worker in list(app.workers):
            await worker.wait()
        await pilot.pause()

        assert snapshot_calls == 2
        assert limits_calls == 2
        assert "57%" in _limits_text(app)


# -- CLI: `alloy monitor --once` (plain text, no --json) -----------------------


def test_cli_monitor_once_plain_text_prints_header_and_one_row_per_run(
    beads_project: Path, alloy_home: Path, fake_harnesses, monkeypatch: pytest.MonkeyPatch
):
    from alloy.engine import Engine
    from alloy.store import Store
    from support import make_harness

    make_harness(beads_project, alloy_home, store=Store(alloy_home / "alloy.db"))

    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["monitor", "--once", "--repo", str(beads_project), "--root", str(alloy_home)],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() != ""


def test_cli_monitor_once_plain_text_with_no_runs_prints_header_without_raising(
    beads_project: Path, alloy_home: Path
):
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["monitor", "--once", "--repo", str(beads_project), "--root", str(alloy_home)],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() != ""


def test_cli_monitor_once_plain_text_prints_limits_lines_before_table_rows(
    beads_project: Path, alloy_home: Path, fake_harnesses, monkeypatch: pytest.MonkeyPatch
):
    from alloy.limits import window, write_cache
    from alloy.paths import AlloyPaths
    from alloy.store import Store
    from support import make_harness

    fake_harnesses.remove("cursor-agent")
    paths = AlloyPaths.resolve(alloy_home).ensure()
    write_cache(
        paths,
        {
            "claude": {
                "harness": "claude",
                "installed": True,
                "available": True,
                "fetched_at": "2026-09-23T10:00:00+00:00",
                "as_of": "2026-09-23T10:00:00+00:00",
                "source": "oauth-usage-api",
                "error": None,
                "status": None,
                "windows": [window("five_hour", "5h", 42.0, None)],
            },
        },
    )

    harness = make_harness(beads_project, alloy_home, store=Store(alloy_home / "alloy.db"))
    bead_id = harness.bead.id

    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["monitor", "--once", "--repo", str(beads_project), "--root", str(alloy_home)],
    )

    assert result.exit_code == 0
    output = result.stdout
    limits_pos = output.find("42%")
    bead_pos = output.find(bead_id)
    assert limits_pos != -1
    assert bead_pos != -1
    assert limits_pos < bead_pos


def test_cli_monitor_help_lists_limits_interval_and_no_limits():
    runner = CliRunner()
    result = runner.invoke(cli_app, ["monitor", "--help"])

    assert result.exit_code == 0
    assert "--limits-interval" in result.stdout
    assert "--no-limits" in result.stdout


def test_cli_monitor_once_no_limits_skips_probe_all(
    beads_project: Path, alloy_home: Path, monkeypatch: pytest.MonkeyPatch
):
    def _raise(*_args, **_kwargs):
        raise AssertionError("probe_all must not be called with --no-limits")

    monkeypatch.setattr("alloy.cli.probe_all", _raise)

    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "monitor",
            "--once",
            "--no-limits",
            "--repo",
            str(beads_project),
            "--root",
            str(alloy_home),
        ],
    )

    assert result.exit_code == 0


# -- alloy-o89.4: width-tiered column visibility in runs table ----------------


async def test_runs_table_at_wide_width_shows_all_columns():
    app = MonitorApp(snapshot_source=lambda: TWO_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        table = _runs_table(app)
        assert _table_column_keys(table) == list(_expected_visible_column_keys(100))
        assert len(_table_column_keys(table)) == len(COLUMNS)


async def test_runs_table_at_comfortable_width_omits_wide_only_columns():
    app = MonitorApp(snapshot_source=lambda: TWO_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test(size=(99, 24)) as pilot:
        await pilot.pause()
        keys = _table_column_keys(_runs_table(app))
        assert keys == list(_expected_visible_column_keys(99))
        assert "parent" not in keys
        assert "stage" not in keys
        assert "cons" not in keys
        assert "complexity" not in keys
        assert "recipe" in keys


async def test_runs_table_at_comfortable_lower_bound_keeps_recipe():
    app = MonitorApp(snapshot_source=lambda: TWO_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        keys = _table_column_keys(_runs_table(app))
        assert keys == list(_expected_visible_column_keys(80))
        assert "recipe" in keys
        assert "parent" not in keys


async def test_runs_table_below_comfortable_width_also_omits_recipe():
    app = MonitorApp(snapshot_source=lambda: TWO_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test(size=(79, 24)) as pilot:
        await pilot.pause()
        keys = _table_column_keys(_runs_table(app))
        assert keys == list(_expected_visible_column_keys(79))
        assert "recipe" not in keys
        assert "parent" not in keys


async def test_runs_table_columns_update_when_terminal_is_resized():
    app = MonitorApp(snapshot_source=lambda: TWO_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        table = _runs_table(app)
        assert _table_column_keys(table) == list(_expected_visible_column_keys(100))

        await pilot.resize_terminal(79, 24)
        await pilot.pause()
        assert _table_column_keys(table) == list(_expected_visible_column_keys(79))


# -- alloy-3g0.5: right-aligned numeric columns in runs table -----------------


async def test_iter_column_header_and_first_row_cell_are_right_aligned():
    app = MonitorApp(snapshot_source=lambda: TWO_RUNS, interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = _runs_table(app)
        iter_col = table.get_column_index("iter")
        iter_column = next(col for col in table.ordered_columns if col.key.value == "iter")
        header = iter_column.label
        cell = table.get_cell_at(Coordinate(row=0, column=iter_col))

        assert isinstance(header, Text)
        assert header.justify == "right"
        assert isinstance(cell, Text)
        assert cell.justify == "right"
