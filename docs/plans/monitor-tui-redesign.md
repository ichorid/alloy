# Monitor TUI redesign — btop/htop-style chrome

Source: chat design session, 2026-09-23. `src/alloy/monitor/` currently has
no `.tcss`/CSS at all (confirmed by grep) — `MonitorApp` (`src/alloy/monitor/app.py`)
renders `Header`, `Static#stats`, `Static#limits`, `DataTable#runs`,
`Static#detail`, `Footer` with Textual's stock default styling. `render.py`
holds the pure snapshot→text functions (`header_line`, `run_rows`,
`limits_lines`/`_limits_line`, `detail_lines`, `COLUMNS`) that feed those
widgets; each is unit-testable without a running Textual app, per
`tests/test_monitor_render.py`.

## Palette (dark, Monokai-ish)

- panel bg `#12161d`, page bg `#0a0d12`, border `#2a3038`
- text primary `#c9d1d9`, dim `#6b7280`
- accent (title/border-label/footer key chips) `#79c0ff`
- status colors: running `#56b6c2` (cyan), judge `#d2a8ff` (magenta),
  blocked `#f85149` (red), done `#7ee787` (green), ready `#8b949e` (dim gray)
- limits threshold colors: `<50%` green `#7ee787`, `50–79%` yellow `#e3b341`,
  `>=80%` red `#f85149`

## Panel chrome

Each of the stats/limits/runs/detail panels gets a bordered box with its
label cut into the top border, btop-style: `┤ LIMITS ├`. Footer keybindings
render as reverse-video key chips (`[j/k] move`) instead of Textual's default
footer text. See the ASCII mockups in the chat transcript for the full
100-col layout; this doc captures the parts that need to survive into code.

## Limits panel: colored threshold bars

Each harness window (`_limits_line` today) becomes a bracketed bar
(`[████████░░░░░░░░] 42%`) colored by the threshold above, instead of plain
`"{label} {pct}%"` text. `[stale]` renders as a small dim-yellow badge.
Unavailable harnesses render their error in red.

## Runs table: status-colored badges

The `status` column (one of `COLUMNS`) gets a colored pill/text per the
status-color mapping above, instead of plain text.

## Selected-row marker

The cursor row in `DataTable#runs` gets a `▶` marker and accent-colored left
border/highlight (Textual's default row cursor highlighting is the fallback
if a custom marker turns out not to be worth the complexity — the important
part is the row being visually unambiguous, not the specific glyph).

## Horizontal scaling (responsive breakpoints)

The 100-col mockup assumes a comfortable terminal; most real terminals
default to 80 cols, and panes can be narrower still. `COLUMNS` needs a
priority tier (always-shown vs. wide-only vs. comfortable-only) so the same
column list drives table rendering at every width instead of hand-duplicating
layouts:

- **≥100 cols ("comfortable")**: all of `COLUMNS` shown.
- **80–99 cols ("wide")**: drop `parent`, `stage`, `cons`, `complexity`
  (the `now` in-flight-call column moves to the detail pane only).
- **<80 cols ("narrow")**: further drop `recipe`; the detail pane switches
  from a two-column layout to a single stacked column.

The width the app reacts to is `self.size.width` (or an equivalent reactive
on the app/screen); breakpoints should be named constants shared by both the
runs-table column selection and the detail-pane layout switch, not duplicated
threshold literals in two places.

## Nerd Font glyphs

See `monitor-nerd-fonts.md`: glyphs are allowed and encouraged, with `unicode`/`ascii` fallbacks; numeric columns right-aligned.
