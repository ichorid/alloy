"""Monitor icon glyphs and ALLOY_MONITOR_ICONS mode resolution.

Glyph codepoints and fallbacks follow docs/plans/monitor-nerd-fonts.md and the
``G`` / ``STATUS`` tables in docs/plans/monitor-nerd-font-mockup.py.
"""

from __future__ import annotations

import os
from typing import Final

Mode = str  # "nerd" | "unicode" | "ascii"

_VALID_MODES: Final[frozenset[str]] = frozenset({"nerd", "unicode", "ascii"})
_ENV_VAR: Final[str] = "ALLOY_MONITOR_ICONS"

# role -> (nerd, unicode, ascii)
_ICONS: Final[dict[str, tuple[str, str, str]]] = {
    "running": ("\uf04b", "\u25b6", ">"),       # play, ▶
    "judge": ("\uf0e3", "\u2696", "="),         # gavel, ⚖
    "blocked": ("\uf05e", "\u2298", "X"),       # ban, ⊘
    "done": ("\uf00c", "\u2713", "+"),          # check, ✓
    "ready": ("\uf017", "\u25f7", "."),         # clock, ◷
    "failed": ("\uf00d", "\u2717", "x"),        # times, \u2717
    "cancelled": ("\uf05e", "\u2298", "-"),     # ban, \u2298
    "waiting-human": ("\uf007", "\u263a", "?"), # user, \u263a
    "review-ready": ("\uf06e", "\u25c9", "r"),  # eye, \u25c9
    "unknown": ("\uf128", "?", "?"),            # question mark
    "pill_l": ("\ue0b6", "[", "["),
    "pill_r": ("\ue0b4", "]", "]"),
    "arrow": ("\ue0b0", "\u203a", ">"),         # powerline right
    "arrow_thin": ("\ue0b1", "\u203a", ">"),    # powerline thin right
    "test_ok": ("\uf00c", "\u2713", "+"),
    "test_fail": ("\uf00d", "\u2717", "x"),
    "warn": ("\uf071", "\u26a0", "!"),
    "stale": ("\uf1da", "\u21bb", "~"),         # history
    "clock": ("\uf017", "\u25f7", "o"),
    "calendar": ("\uf073", "\u25a6", "d"),     # calendar, ▦
    "branch": ("\ue0a0", "\u2387", "@"),       # powerline branch
    "scheduler": ("\uf111", "\u25cf", "*"),     # dot
    "queue": ("\uf0ae", "\u2630", "#"),         # tasks
    "limits": ("\uf0e4", "\u2696", "L"),        # gauge (reuse scale)
    "runs": ("\uf03a", "\u2630", "R"),          # list
    "detail": ("\uf05a", "\u2139", "i"),        # info
    "tokens": ("\uf1c0", "\u25c6", "T"),         # database
    "complexity": ("\uf2db", "\u2582", "C"),    # chip, ▂
    "keyboard": ("\uf11c", "\u2328", "K"),
    "alloy": ("\uf0c3", "\u2697", "A"),          # flask, ⚗
    "folder": ("\uf07b", "\ud83d\udcc1", "/"),   # folder, 📁
    "refresh": ("\uf021", "\u21bb", "R"),        # sync
    "arrow_left": ("\ue0b2", "\u2039", "<"),     # powerline left
}


def icon(role: str, mode: Mode) -> str:
    """Return the glyph for ``role`` in ``mode`` (nerd, unicode, or ascii)."""
    nerd, unicode_glyph, ascii_glyph = _ICONS.get(role) or _ICONS["unknown"]
    if mode == "nerd":
        return nerd
    if mode == "ascii":
        return ascii_glyph
    return unicode_glyph


def resolve_mode(*, interactive: bool = True) -> Mode:
    """Resolve icon mode from ALLOY_MONITOR_ICONS and context.

    Explicit env values (nerd, unicode, ascii) win regardless of ``interactive``.
    Invalid values fall back to unicode. When unset, the live TUI defaults to
    unicode and ``alloy monitor --once`` defaults to ascii.
    """
    raw = os.environ.get(_ENV_VAR)
    if raw is not None:
        normalized = raw.strip().lower()
        if normalized in _VALID_MODES:
            return normalized
        return "unicode"
    return "unicode" if interactive else "ascii"
