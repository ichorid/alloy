"""Acceptance tests for alloy-yux: resolve landing conflict in tests/test_monitor_render.py.

Main must merge into alloy/alloy-3g0 with render.py and tests/test_monitor_render.py
aligned to main's alloy-gek reset suffixes and calendar icons while keeping the
alloy-3g0.3 nerd limits bar tests. These fail until that merge resolution lands.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MONITOR_RENDER_TESTS = REPO_ROOT / "tests" / "test_monitor_render.py"
MONITOR_RENDER_SRC = REPO_ROOT / "src" / "alloy" / "monitor" / "render.py"


def test_monitor_render_py_has_no_merge_conflict_markers():
    text = MONITOR_RENDER_TESTS.read_text(encoding="utf-8")
    assert "<<<<<<<" not in text
    assert ">>>>>>>" not in text
    assert "\n=======\n" not in text
    # Post-merge resolution: nerd limits block precedes alloy-gek helpers (main's layout).
    assert text.index("# -- alloy-3g0.3: nerd limits bars") < text.index("# -- alloy-gek: consumed Codex quota")


def test_monitor_render_py_section_order_nerd_block_before_gek():
    text = MONITOR_RENDER_TESTS.read_text(encoding="utf-8")
    nerd_pos = text.index("# -- alloy-3g0.3: nerd limits bars")
    gek_pos = text.index("# -- alloy-gek: consumed Codex quota")
    assert nerd_pos < gek_pos


def test_monitor_render_py_combines_nerd_and_gek_sections_after_merge():
    text = MONITOR_RENDER_TESTS.read_text(encoding="utf-8")
    assert "# -- alloy-3g0.3: nerd limits bars" in text
    assert "test_usage_bar_34_percent_nerd_uses_eighth_blocks_without_brackets" in text
    assert "# -- alloy-gek: consumed Codex quota + compact local reset suffix" in text
    assert "def _local_tz_amsterdam" in text
    assert "def test_limits_line_weekly_date_suffix_uses_calendar_icon_not_clock" in text


def test_monitor_render_py_gek_weekly_date_suffix_expects_sep_format_not_iso():
    text = MONITOR_RENDER_TESTS.read_text(encoding="utf-8")
    assert 'assert "(Sep 25)" in line' in text
    assert 'assert "(2026-09-25)" in line' not in text


def test_monitor_render_py_codex_rollout_test_matches_main_used_percent_name():
    text = MONITOR_RENDER_TESTS.read_text(encoding="utf-8")
    assert "def test_limits_lines_codex_rollout_shows_used_percent_with_compact_reset_suffix" in text
    assert "def test_limits_lines_codex_rollout_shows_consumed_percent_with_compact_reset_suffix" not in text


def test_render_py_reset_suffix_uses_compact_month_day_for_weekly_cycle():
    text = MONITOR_RENDER_SRC.read_text(encoding="utf-8")
    assert "strftime('%b')" in text or 'strftime("%b")' in text
    assert "Jul 19" in text or "no year" in text.lower()
    assert 'return local.strftime("%Y-%m-%d")' not in text


def test_render_py_nerd_reset_glyph_uses_calendar_for_non_clock_suffix():
    text = MONITOR_RENDER_SRC.read_text(encoding="utf-8")
    assert '"calendar"' in text
    assert '":" in reset_suffix' in text or ':" in reset_suffix' in text


def test_reset_suffix_weekly_other_local_day_returns_sep_compact_date(
    monkeypatch: pytest.MonkeyPatch,
):
    from test_monitor_render import _AMSTERDAM, _freeze_render_now

    from alloy.monitor.render import _reset_suffix

    _freeze_render_now(monkeypatch, datetime(2026, 9, 24, 10, 0, tzinfo=_AMSTERDAM))
    suffix = _reset_suffix("weekly", "2026-09-24T23:00:00+00:00")

    assert suffix == "Sep 25"
    assert "2026" not in suffix


def test_limits_line_weekly_date_suffix_uses_calendar_icon_not_clock(
    monkeypatch: pytest.MonkeyPatch,
):
    from alloy.limits import window
    from alloy.monitor.render import limits_lines

    from test_monitor_render import _AMSTERDAM, _available_claude_limits, _freeze_render_now, _snapshot

    _freeze_render_now(monkeypatch, datetime(2026, 9, 24, 10, 0, tzinfo=_AMSTERDAM))
    snapshot = _snapshot()
    snapshot["limits"] = _available_claude_limits(
        window("seven_day", "weekly", 34.0, "2026-09-24T23:00:00+00:00"),
    )

    line = limits_lines(snapshot, mode="nerd")[0]

    assert "\uf073 Sep 25" in line
    assert "\uf017" not in line
    assert "2026" not in line


def test_codex_probe_used_percent_is_consumed_fraction_from_rollout(tmp_path):
    from alloy.limits.codex import probe

    from test_limits_codex import (
        CODEX_TS,
        RESETS_AT_EPOCH,
        _primary,
        _token_count_line,
        write_rollout,
    )

    codex_home = tmp_path / "home"
    write_rollout(
        codex_home,
        "session-a",
        "rollout-a.jsonl",
        [
            _token_count_line(
                CODEX_TS,
                limit_id="codex",
                primary=_primary(53, 300, RESETS_AT_EPOCH),
                secondary=_primary(51, 10080, RESETS_AT_EPOCH),
            ),
        ],
    )
    sample = probe(codex_home)

    assert sample["windows"][0]["used_percent"] == 53.0
