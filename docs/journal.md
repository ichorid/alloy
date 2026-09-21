# Journal of unexpected things

Everything that surprised us while using Alloy to implement its own execution
monitor (the `alloy-626` / `alloy-73m` / `alloy-44s` beads). One entry per
surprise, newest at the bottom. Each entry says what happened, why it matters,
and what was done about it -- so the list doubles as a hardening backlog.

Status legend: **fixed** (in this repo), **mitigated** (worked around, root
cause elsewhere), **open** (nothing done yet), **external** (not Alloy's bug).

---

## 1. `bd create -f` only splits on H2 headings -- external

The markdown importer treats every `## ` as a new issue and ignores `### `
boundaries as issue separators (they become field sections: Description,
Design, Acceptance Criteria, Priority, Type, Labels). A plan document with
H2 narrative sections therefore has to keep its bead sections in a separate
file (`docs/plans/execution-monitor.beads.md`) or every narrative H2 becomes
a bogus issue.

## 2. `bd create -f` does not resolve same-file dependencies -- external

`blocked-by:<slugified-title>` inside the imported file creates a dependency
edge whose `depends_on_id` is the literal slug string, not the real id of the
sibling bead created from the same file (`bd show` reports
`dependency_count: 0`, the raw `dependencies` array carries the slug). Beads'
own readiness graph then ignores it. Workaround: import without inline
dependencies and link afterwards with `bd dep add <child> --blocked-by <id>`.

## 3. `bd init` has side effects on the git repo -- external

It auto-commits (`bd init: initialize beads issue tracking`) and configures a
remote for `refs/dolt/data`. Fine, but not what one expects from "init".

## 4. Recipe YAML discovery and the graph registry are two systems -- fixed

`alloy.config.discover_recipes` finds YAML files; `alloy.recipes.REGISTRY`
maps names to Python graph builders. Adding `tdd-loop-jev.yaml` without a
registry entry produced `KeyError: unknown recipe graph 'tdd-loop-jev'` --
*after* the bead had been claimed. `alloy recipes --json` had been reporting
`"graph": false` all along; nobody read it. Fixed by registering the recipe,
and (see 5) by validating both halves before a bead is claimed.

## 5. Crashing between claim and run record strands the bead -- fixed

`Engine.run` moved the bead `open -> implementing` and only then built the
context (recipe lookup, config load, worktree). A failure there left the bead
`implementing` with no `runs` row, so `alloy status` showed nothing and
`Scheduler.recover()` could not see it either; manual `bd update -s open` was
the only way out. Fixed: recipe graph + config are resolved before the claim,
and any failure before `create_run` returns the bead to `open`.

## 6. `alloy status` showed a hardcoded runner -- fixed

The status table always printed the *implement* role's runner (`astra`)
whatever the stage was. Fixed in two steps; the first fix (prefer the last
completed agent call) was itself wrong -- it showed `tests:claude` while the
stage was already `implement`, because the implement call had not returned
yet. Current rule: the stage's configured role wins when the stage names an
agent role; only deterministic stages fall back to the last completed call.

## 7. `alloy cancel` did not stop anything -- fixed

It only rewrote the ledger and bead status; the `alloy run` process and its
harness child kept working. Fixed: cancel now signals the owning process
(SIGTERM, then SIGKILL after a grace period) when it is alive.

## 8. Killing `alloy run` orphans the harness process tree -- fixed

`CLIRunner.run` spawned the harness in the parent's process group, so a
SIGTERM/SIGKILL to `alloy run` (or an `asyncio` cancellation) left the codex
CLI running to completion on its own. Timeouts had the same flaw:
`process.kill()` only reached the immediate child (the node wrapper), not the
native binary underneath it. Fixed: every harness and every test command runs
in its own session/process group, and timeout, cancellation and signals kill
the whole group.

## 9. Codex's sandbox helpers outlive their parent -- mitigated

`codex-linux-sandbox` supervisor processes (one per shell tool call) are
reparented to `systemd --user` when the codex CLI dies and keep running the
command they were supervising (in our case a 3-minute pytest). Process-group
killing (entry 8) covers the ones still in the group; the rest need
`pkill -f codex-linux-sandbox`. Root cause is inside codex.

## 10. Jev's API key block was commented out -- fixed

`tdd-loop-jev.yaml` shipped the `runners: jev: api_key_file:` block as a
commented example, so every real judge call failed with "no API key" and
silently became `retry`, burning an iteration each time. Alloy handled the
failure exactly as designed -- which is why nobody noticed for 50 minutes.
Fixed by activating the block; see also 17.

## 11. Worktree tests were testing main's code -- fixed (critical)

The venv installs `alloy` in editable mode via a `.pth` file that points at
`/home/vader/MY_SRC/alloy/src`. Running `python -m pytest` inside
`~/.alloy/worktrees/<bead>` therefore imports the **main checkout's**
package, not the worktree's. Every verification Alloy ran, and every test
the implementer ran, exercised code the implementer had not touched. The
codex agent worked it out on its own and prefixed `PYTHONPATH=src`. Fixed in
two layers: `pyproject.toml` sets `pythonpath = ["src"]` for pytest (pytest
puts it ahead of site-packages), and `alloy.verify.run_tests` prepends the
worktree's `src/` and root to `PYTHONPATH` so the same holds for any Python
project regardless of its pytest config.

## 12. The codex sandbox fights the task -- mitigated

Inside `codex exec --sandbox workspace-write` the agent (a) could not use
`bd` because the Dolt database lives in the main repo, outside the writable
root, and the repository's own instructions told it to track work in Beads;
(b) reported that asyncio's self-pipe socket writes were denied, stalling
`aiosqlite` and hence the whole integration suite, and spent a good part of a
22-minute implement call building a launcher to work around it. Mitigation:
the implementer prompt now says Alloy owns task tracking and verification, so
the agent stops trying to run `bd` or the full suite itself. The asyncio
symptom is filed for codex.

## 13. pytest summary parsing double-counted errors -- fixed

`parse_counts` scanned the whole output for `N error`, so `1 error during
collection` plus `1 error in 0.24s` became "2 failed". Fixed to parse the
final summary line only.

## 14. Wall-time limit counted time waiting for a human -- fixed

`RunContext.elapsed()` measured from `runs.started_at`, so a run parked at a
human gate overnight breached `max_wall_time` the moment it resumed, and
would pause again immediately. Fixed: the ledger accumulates paused time and
`elapsed()` subtracts it.

## 15. No logging anywhere -- fixed

`Scheduler.recover()`/`tick()` swallowed every exception silently and the
detached scheduler sent stdout/stderr to `/dev/null`. Fixed: a `logging`
logger in engine/scheduler/runtime, and the detached scheduler appends to
`~/.alloy/scheduler.log`.

## 16. `guard` and `finish` never updated the persisted stage -- fixed

A finished run kept `stage = "judge"` in the ledger. Fixed by setting the
stage in `finish` (and `human_gate`).

## 17. Two processes can run the same bead -- fixed

`Engine.run`/`resume` only refused a run when its status was terminal; a run
`running` with a *live* pid slipped through and a second process would start
work in the same worktree. Fixed: both refuse while the owning pid is alive.

## 18. A failed judge burns an iteration blind -- fixed

When the judge runner fails (Jev without a key, Claude overloaded) the loop
retries with "Judge was unavailable; continue from the test output." Fixed by
per-role fallbacks: `RoleSpec.fallback` names a second runner/model used when
the primary is unavailable or exits non-zero. The recipes now fall back to
Claude Fable 5.1 for implementation (`claude-write`, model `fable`) and to
Claude for judging in the Jev recipe.

## 19. Transcript filenames carry the end time -- fixed

`_write_log` timestamped files when the call finished, so a 22-minute codex
call sorted *after* the test log written one second after it. Fixed to use
the start time.

## 20. `git add -A --intent-to-add` sweeps junk into the judge's diff -- fixed

`WorktreeManager.diff()` stages every untracked file so the judge sees new
files. A stray `.serena/` directory (created by an IDE tool) was therefore
part of the diff the judge evaluated. `.serena/` is now gitignored.

## 21. The monitor plan reinvented a TUI framework -- fixed

The interactive view was specified as `rich.live.Live` plus hand-rolled
`termios` cbreak mode, a key-reading thread and a queue. Textual (same
author as Rich) provides all of it -- widgets, bindings, timers, thread
workers, resize handling -- and, decisively, a headless `App.run_test()` /
`Pilot` harness that makes the acceptance tests possible without a terminal.
Plan and beads rewritten around Textual; the view bead split into two smaller
ones.

## 22. The implementer runs the full suite itself, repeatedly -- open

The codex implement call ran `pytest -q` for the whole repo several times
(146 shell commands, 22 minutes). The suite includes real `bd init`,
SIGKILL-and-resume and 8-second timeouts, so it is slow. Cheap mitigation
applied via the prompt (Alloy verifies); the structural fix is a faster
suite or a `verify.command` that runs only the relevant tests during
iterations.

## 23. The tests role can be wrong and only the implementer notices -- open

Claude wrote a test asserting the checkpoint stage after a human gate is
`waiting-human`; it is `guard` (the ledger says `waiting-human`, the graph
does not). Codex corrected the assertion and said so, as the prompt allows.
The judge should be told explicitly when tests were edited; today it can
only infer it from the diff.
