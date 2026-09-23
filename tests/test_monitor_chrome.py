"""Panel border chrome and footer styling for `MonitorApp` (alloy-o89.1, alloy-3g0.4).

Behavioral/id-based checks only: widget ids, border titles/subtitles, loaded
stylesheet, palette tokens in TCSS, and footer key chip text. Does not assert
geometry or full-screen snapshots.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, Footer, Static

from alloy.monitor import app as app_module
from alloy.monitor.app import MonitorApp

DISABLED_INTERVAL = 1000.0

PANEL_IDS = ("stats", "limits", "runs", "detail")
PANEL_TITLES = {
    "stats": "STATS",
    "limits": "LIMITS",
    "runs": "RUNS",
    "detail": "DETAIL",
}
PALETTE = {
    "page": "#0a0d12",
    "panel": "#12161d",
    "border": "#2a3038",
    "accent": "#79c0ff",
}


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


def _run(run_id: str) -> dict:
    return {
        "bead_id": f"alloy-{run_id}",
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
        "tokens": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": None,
        },
        "tokens_by_role": {},
        "judge": None,
        "worktree": f"/home/vader/.alloy/worktrees/{run_id}",
        "branch": f"alloy/{run_id}",
    }


def _claude_limits(used_percent: float) -> dict:
    from alloy.limits import window

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


def _monitor_tcss_path() -> Path:
    return Path(app_module.__file__).with_name("monitor.tcss")


def _panel_widget(app: MonitorApp, panel_id: str) -> Static | DataTable:
    if panel_id == "runs":
        return app.query_one("#runs", DataTable)
    return app.query_one(f"#{panel_id}", Static)


def _border_is_round(widget: Static | DataTable) -> bool:
    border = widget.styles.border
    return all(getattr(border, edge)[0] == "round" for edge in ("top", "right", "bottom", "left"))


def _footer_plain_text(app: MonitorApp) -> str:
    footer = app.query_one(Footer)
    rendered = footer.render()
    return getattr(rendered, "plain", str(rendered))


def _footer_key_plain_texts(app: MonitorApp) -> list[str]:
    footer = app.query_one(Footer)
    texts: list[str] = []
    for child in footer.children:
        rendered = child.render()
        plain = getattr(rendered, "plain", str(rendered))
        texts.append(plain)
    return texts


# -- CSS_PATH and monitor.tcss -------------------------------------------------


def test_monitor_app_declares_css_path_for_monitor_tcss():
    assert getattr(MonitorApp, "CSS_PATH", None) == "monitor.tcss"


def test_monitor_tcss_exists_beside_app_module():
    assert _monitor_tcss_path().is_file()


def test_monitor_tcss_styles_each_panel_selector_with_border():
    content = _monitor_tcss_path().read_text(encoding="utf-8").lower()
    for panel_id in PANEL_IDS:
        assert f"#{panel_id}" in content
        panel_block = content.split(f"#{panel_id}", 1)[1].split("}", 1)[0]
        assert "border" in panel_block


def test_monitor_tcss_declares_round_border_on_each_panel():
    content = _monitor_tcss_path().read_text(encoding="utf-8").lower()
    for panel_id in PANEL_IDS:
        panel_block = content.split(f"#{panel_id}", 1)[1].split("}", 1)[0]
        assert "border: round" in panel_block


def test_monitor_tcss_declares_the_dark_palette_tokens():
    content = _monitor_tcss_path().read_text(encoding="utf-8").lower()
    for token in PALETTE.values():
        assert token in content


# -- panel border titles and chrome --------------------------------------------


@pytest.mark.parametrize("panel_id,title", list(PANEL_TITLES.items()))
async def test_each_panel_widget_has_its_border_title(panel_id: str, title: str):
    limits = _claude_limits(42.0)
    snapshot = _snapshot(limits=limits, runs=[_run("run-1")])
    app = MonitorApp(
        snapshot_source=lambda: snapshot,
        limits_source=lambda: limits,
        interval=DISABLED_INTERVAL,
        limits_interval=DISABLED_INTERVAL,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        widget = _panel_widget(app, panel_id)
        assert widget.border_title == title


@pytest.mark.parametrize("panel_id", PANEL_IDS)
async def test_each_panel_widget_has_a_round_border_after_mount(panel_id: str):
    limits = _claude_limits(42.0)
    snapshot = _snapshot(limits=limits, runs=[_run("run-1")])
    app = MonitorApp(
        snapshot_source=lambda: snapshot,
        limits_source=lambda: limits,
        interval=DISABLED_INTERVAL,
        limits_interval=DISABLED_INTERVAL,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        widget = _panel_widget(app, panel_id)
        assert _border_is_round(widget)


async def test_limits_panel_border_title_includes_nerd_glyph_in_nerd_mode(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ALLOY_MONITOR_ICONS", "nerd")
    limits = _claude_limits(42.0)
    snapshot = _snapshot(limits=limits, runs=[_run("run-1")])
    app = MonitorApp(
        snapshot_source=lambda: snapshot,
        limits_source=lambda: limits,
        interval=DISABLED_INTERVAL,
        limits_interval=DISABLED_INTERVAL,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        limits_widget = _panel_widget(app, "limits")
        title = limits_widget.border_title or ""
        assert "\uf0e4" in title
        assert "limits" in title.lower()


async def test_runs_panel_border_subtitle_shows_run_count():
    limits = _claude_limits(42.0)
    runs = [_run(f"run-{index}") for index in range(5)]
    snapshot = _snapshot(limits=limits, runs=runs)
    app = MonitorApp(
        snapshot_source=lambda: snapshot,
        limits_source=lambda: limits,
        interval=DISABLED_INTERVAL,
        limits_interval=DISABLED_INTERVAL,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        runs_widget = _panel_widget(app, "runs")
        subtitle = runs_widget.border_subtitle or ""
        assert "5 runs" in subtitle


async def test_panel_widgets_use_the_dark_panel_background_color():
    limits = _claude_limits(42.0)
    snapshot = _snapshot(limits=limits, runs=[_run("run-1")])
    app = MonitorApp(
        snapshot_source=lambda: snapshot,
        limits_source=lambda: limits,
        interval=DISABLED_INTERVAL,
        limits_interval=DISABLED_INTERVAL,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        for panel_id in PANEL_IDS:
            widget = _panel_widget(app, panel_id)
            assert widget.styles.background.hex.lower() == PALETTE["panel"]


async def test_screen_uses_the_dark_page_background_color():
    app = MonitorApp(snapshot_source=lambda: _snapshot(), interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen.styles.background.hex.lower() == PALETTE["page"]


# -- footer key chips ----------------------------------------------------------


async def test_footer_renders_combined_j_k_move_chip_instead_of_stock_down_up():
    app = MonitorApp(snapshot_source=lambda: _snapshot(runs=[_run("run-1")]), interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        footer_texts = _footer_key_plain_texts(app)
        assert any("[j/k]" in text and "move" in text for text in footer_texts)
        assert not any(text.strip() in {"j Down", "k Up"} for text in footer_texts)


async def test_footer_shows_icons_mode_marker_in_nerd_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALLOY_MONITOR_ICONS", "nerd")
    app = MonitorApp(snapshot_source=lambda: _snapshot(runs=[_run("run-1")]), interval=DISABLED_INTERVAL)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "icons: nerd" in _footer_plain_text(app)
