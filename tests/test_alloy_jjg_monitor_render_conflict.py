"""Acceptance tests for alloy-jjg: unresolved merge markers in tests/test_monitor_render.py.

Commit f981690 left ``<<<<<<< HEAD`` / ``=======`` / ``>>>>>>> main`` in the merged
test module, so pytest collection raised SyntaxError during parallel regression.
Behaviour is not implemented yet — these must fail until the landing-conflict
resolution keeps both alloy-3g0.3 nerd limits coverage and alloy-gek compact
reset-suffix coverage in ``tests/test_monitor_render.py`` without conflict markers.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MONITOR_RENDER_TESTS = REPO_ROOT / "tests" / "test_monitor_render.py"

NERD_GEK_PARALLEL_KERNEL = (
    "test_usage_bar_34_percent_nerd_uses_eighth_blocks_without_brackets or "
    "test_limits_line_weekly_reset_suffix_other_local_day_shows_date or "
    "test_limits_lines_codex_rollout_shows_used_percent_with_compact_reset_suffix or "
    "test_limits_line_weekly_date_suffix_uses_calendar_icon_not_clock"
)


def test_monitor_render_py_has_no_literal_git_conflict_markers():
    text = MONITOR_RENDER_TESTS.read_text(encoding="utf-8")
    assert "<<<<<<< HEAD" not in text
    assert ">>>>>>> main" not in text
    assert "<<<<<<<" not in text
    assert ">>>>>>>" not in text
    assert "\n=======\n" not in text


def test_monitor_render_py_parses_as_python_module():
    source = MONITOR_RENDER_TESTS.read_text(encoding="utf-8")
    ast.parse(source, filename=str(MONITOR_RENDER_TESTS))


def test_monitor_render_py_py_compile_subprocess_succeeds():
    proc = subprocess.run(
        ["uv", "run", "python", "-m", "py_compile", str(MONITOR_RENDER_TESTS)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_monitor_render_py_combines_nerd_and_gek_sections_after_merge():
    text = MONITOR_RENDER_TESTS.read_text(encoding="utf-8")
    assert "# -- alloy-3g0.3: nerd limits bars" in text
    assert "test_usage_bar_34_percent_nerd_uses_eighth_blocks_without_brackets" in text
    assert "# -- alloy-gek: consumed Codex quota + compact local reset suffix" in text
    assert "def _local_tz_amsterdam" in text
    assert "def _freeze_render_now" in text
    assert "def _limits_line_for_single_window" in text
    assert "def test_limits_line_weekly_date_suffix_uses_calendar_icon_not_clock" in text


def test_monitor_render_py_gek_weekly_date_suffix_expects_sep_format_not_iso():
    text = MONITOR_RENDER_TESTS.read_text(encoding="utf-8")
    assert 'assert "(Sep 25)" in line' in text
    assert 'assert "(2026-09-25)" in line' not in text


def test_monitor_render_py_codex_rollout_test_matches_main_used_percent_name():
    text = MONITOR_RENDER_TESTS.read_text(encoding="utf-8")
    assert "def test_limits_lines_codex_rollout_shows_used_percent_with_compact_reset_suffix" in text
    assert "def test_limits_lines_codex_rollout_shows_consumed_percent_with_compact_reset_suffix" not in text


def test_monitor_render_py_hosts_reset_suffix_weekly_compact_unit_test():
    """Merge resolution keeps _reset_suffix coverage in the merged module, not only landing_merge."""
    text = MONITOR_RENDER_TESTS.read_text(encoding="utf-8")
    assert "def test_reset_suffix_weekly_other_local_day_returns_sep_compact_date(" in text


def test_monitor_render_py_parallel_kernel_regression_exits_zero():
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pytest",
            "-n",
            "0",
            "-q",
            str(MONITOR_RENDER_TESTS),
            "-k",
            NERD_GEK_PARALLEL_KERNEL,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SyntaxError" not in proc.stdout + proc.stderr
    assert "FAILED" not in proc.stdout


def test_monitor_render_py_full_module_parallel_regression_exits_zero():
    proc = subprocess.run(
        ["uv", "run", "pytest", "-n", "0", "-q", str(MONITOR_RENDER_TESTS)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SyntaxError" not in proc.stdout + proc.stderr
    assert "error during collection" not in (proc.stdout + proc.stderr).lower()
