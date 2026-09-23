"""`alloy.monitor.icons`: role glyph table and ALLOY_MONITOR_ICONS mode resolution."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from alloy.cli import app as cli_app
from alloy.monitor.icons import icon, resolve_mode

MODES = ("nerd", "unicode", "ascii")

REQUIRED_ROLES = (
    "running",
    "judge",
    "blocked",
    "done",
    "ready",
    "pill_l",
    "pill_r",
    "arrow",
    "arrow_thin",
    "test_ok",
    "test_fail",
    "warn",
    "stale",
    "clock",
    "branch",
    "scheduler",
    "queue",
    "limits",
    "runs",
    "detail",
    "tokens",
    "complexity",
    "keyboard",
)

PRIVATE_USE_MIN = 0xE000
PRIVATE_USE_MAX = 0xF8FF


@pytest.fixture
def unset_monitor_icons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOY_MONITOR_ICONS", raising=False)


def _contains_private_use_area(text: str) -> bool:
    return any(PRIVATE_USE_MIN <= ord(ch) <= PRIVATE_USE_MAX for ch in text)


@pytest.mark.parametrize("role", REQUIRED_ROLES)
def test_icon_ascii_is_pure_ascii(role: str) -> None:
    value = icon(role, "ascii")
    assert value
    assert value.isascii()


@pytest.mark.parametrize("role", REQUIRED_ROLES)
@pytest.mark.parametrize("mode", MODES)
def test_icon_nonempty_for_every_mode(role: str, mode: str) -> None:
    assert icon(role, mode)


def test_icon_running_nerd_is_play_glyph() -> None:
    assert icon("running", "nerd") == "\uf04b"


def test_icon_done_nerd_is_check_glyph() -> None:
    assert icon("done", "nerd") == "\uf00c"


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("nerd", "nerd"),
        ("unicode", "unicode"),
        ("ascii", "ascii"),
        ("NERD", "nerd"),
        ("Unicode", "unicode"),
        ("ASCII", "ascii"),
    ],
)
def test_resolve_mode_honours_valid_env(
    monkeypatch: pytest.MonkeyPatch, env_value: str, expected: str
) -> None:
    monkeypatch.setenv("ALLOY_MONITOR_ICONS", env_value)
    assert resolve_mode(interactive=True) == expected
    assert resolve_mode(interactive=False) == expected


@pytest.mark.parametrize("interactive", [True, False])
def test_resolve_mode_invalid_env_returns_unicode(
    monkeypatch: pytest.MonkeyPatch, interactive: bool
) -> None:
    monkeypatch.setenv("ALLOY_MONITOR_ICONS", "bogus")
    assert resolve_mode(interactive=interactive) == "unicode"


def test_resolve_mode_unset_returns_unicode_when_interactive(
    unset_monitor_icons: None,
) -> None:
    assert resolve_mode(interactive=True) == "unicode"


def test_resolve_mode_unset_returns_ascii_when_not_interactive(
    unset_monitor_icons: None,
) -> None:
    assert resolve_mode(interactive=False) == "ascii"


def test_cli_monitor_once_unset_env_emits_no_private_use_glyphs(
    beads_project,
    alloy_home,
    unset_monitor_icons: None,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["monitor", "--once", "--repo", str(beads_project), "--root", str(alloy_home)],
    )

    assert result.exit_code == 0
    assert not _contains_private_use_area(result.stdout)
