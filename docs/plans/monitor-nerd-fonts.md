# Monitor: Nerd Font glyphs

Source: chat design session, 2026-09-24. Visual reference:
`docs/plans/monitor-nerd-font-mockup.ansi` (view with `cat`; needs a Nerd Font
terminal). It complements `monitor-tui-redesign.md`.

**The project owner's terminal has a Nerd Font installed. Using its glyphs in
`alloy monitor` is allowed and encouraged.** Do not restrict new monitor work
to plain ASCII/box-drawing just to be safe.

## Rules

- Glyph choices live in one place (an icon table in `render.py`, keyed by
  status/role), never as literals scattered through render functions.
- Add an icon-mode switch, `ALLOY_MONITOR_ICONS=nerd|unicode|ascii`. Every icon
  needs a `unicode` and an `ascii` fallback so SSH/tmux/CI/`--once` output does
  not show tofu. Tests must cover all three modes; default in tests is `ascii`.
- Never rely on color alone: keep the status word next to its icon.
- Some glyphs are double-width. Pad and align on cell width (Rich
  `cell_len`), not `len()`.
- Numeric columns (`iter`, `tests`, `elapsed`, tokens) are right-aligned;
  text columns (`bead`, `status`, `stage`, `now`) are left-aligned.

## Glyph vocabulary (codepoints)

| Use | Glyph | Codepoint |
|---|---|---|
| pill caps | left / right | U+E0B6 / U+E0B4 |
| header separator | powerline arrow | U+E0B0 |
| running / judge / blocked / done / ready | play / gavel / ban / check / clock | U+F04B / U+F0E3 / U+F05E / U+F00C / U+F017 |
| test pass / fail | check / cross | U+F00C / U+F00D |
| limit warning (>=80%) | warning | U+F071 |
| reset time | clock | U+F017 |
| branch, tokens, complexity | git-branch / database / microchip | U+E725 / U+F1C0 / U+F2DB |

Limit bars use fractional blocks (U+258F..U+2589) instead of `[███░░░]`.
Keep the threshold-color rule from `monitor-tui-redesign.md`: the color tag
wraps the whole segment (label + percent + bar).
