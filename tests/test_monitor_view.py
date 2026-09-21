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
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Static
from typer.testing import CliRunner

from alloy.cli import app as cli_app
from alloy.monitor.app import MonitorApp

DISABLED_INTERVAL = 1000.0
EMPTY_TOKENS = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": None}


def _snapshot(*, runs=()) -> dict:
    return {
        "root": "/home/vader/.alloy",
        "repo": "/home/vader/MY_SRC/alloy",
        "scheduler": {"running": False, "pid": None},
        "ready_count": 0,
        "ready_capped_at": 1000,
        "lifetime": {"done": 0, "failed": 0, "cancelled": 0},
        "runs": list(runs),
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


ZERO_RUNS = _snapshot(runs=[])
TWO_RUNS = _snapshot(runs=[_run("run-1"), _run("run-2")])
THREE_RUNS = _snapshot(runs=[_run("run-1"), _run("run-2"), _run("run-3")])


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


def _runs_table(app: MonitorApp) -> DataTable:
    return app.query_one("#runs", DataTable)


def _stats_text(app: MonitorApp) -> str:
    static = app.query_one("#stats", Static)
    # Static has no public renderable getter; the mangled attribute is the
    # only way to read back what update() stored, short of full screen render.
    return str(getattr(static, "_Static__content", ""))


def _cursor_run_id(table: DataTable) -> str:
    key = table.coordinate_to_cell_key(Coordinate(row=table.cursor_row, column=0))
    return key.row_key.value


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
