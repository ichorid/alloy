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

## 24. Adopting a worktree used HEAD as the diff base -- fixed

`WorktreeManager.ensure()` returned `base_commit = HEAD` when it adopted an
existing worktree on resume. Harmless while agents never commit, but the
moment the operator merges `main` forward into the bead branch (needed here,
so the worktree gets the pytest `pythonpath` fix and the new prompts) HEAD
moves and the judge's diff would have shown only the uncommitted remainder.
Fixed: the base is the merge-base of the branch and the repository's HEAD.

## 25. `alloy recipes` called Jev "missing" while it worked -- fixed

The command built a bare `RunnerRegistry()` to answer "is this runner
available?", ignoring the recipe's own `runners:` block -- the one place the
Jev key file is configured. Fixed: availability is evaluated with the
recipe's overrides, and the output now shows each role's fallback chain.

## 26. Time spent dead counted as wall time -- fixed

The `alloy-626` run was killed at minute ~41 and adopted at minute ~78; with
a 90-minute limit the resumed run would have hit `max_wall_time` almost
immediately. Fixed: adopting an orphaned run banks the interval since the
dead owner's last write as paused time.

## 27. Adopting a run that never checkpointed crashed it -- fixed

A run killed during its very first node (`context`) has a `runs` row but no
LangGraph checkpoint. Recovery handed the graph `None` to "continue", and
LangGraph raised `EmptyInputError`, which the engine then recorded as a
crash -- the bead went `failed` for having been unlucky twice. Fixed: with no
checkpoint, the adopted run starts from the recipe's initial state.

## 28. codex's children leave the process group -- fixed

Watching the resumed run: the node wrapper and the native codex binary
share the session and group Alloy created, but codex's own children -- its
MCP servers and `codex-linux-sandbox` -- call `setpgid` and sit in groups of
their own. A plain `killpg` (entry 8) would have left them running. Fixed:
`terminate_process_tree` walks `/proc` for every process in the session and
every descendant by parent id, signals them all, and sweeps survivors with
SIGKILL after the grace period. Covered by a test with a grandchild in its
own group.

## 29. Codex ran out of credits mid-run; the fallback carried on -- fixed/observed

Iteration 2's codex call ended after 101 s with `turn.failed: Your workspace
is out of credits`. The per-role fallback (entry 18) switched to Claude Fable
without operator involvement -- the first real exercise of that path, one
hour after it was written. Left as designed.

## 30. The recorded error was stderr noise, not the reason -- fixed

For that same failure the ledger said `Reading additional input from
stdin...` -- codex's first stderr line -- because `AgentResult.error`
preferred stderr over the parsed answer. The parsed answer *was* the JSONL
`error` event with the real message. Fixed: the failure message is the
parsed text first, then stderr.

## 31. First bead through the whole loop -- observed

`alloy-626` finished at 23:32: 5 agent calls plus the fallback's, 165 tests
green, Jev's verdict `done` with p=0.62 (retry 0.27, human 0.10) and a
reported confidence of 0.51, on 6.8k input tokens in one second. The judge
gave no reason and no instructions -- by design for Jev -- so `alloy run`
printed `done alloy-626 --` with an empty reason. Worth showing the
probabilities in that line instead (idea for the monitor's judge panel,
already in the plan). Integration was manual: commit the worktree on its
branch, `git merge --no-ff` into main, full suite, `bd close`.

## 32. Fable fixed a spec change made mid-run -- observed

The `cost_usd` rule was added to the bead's acceptance criteria while the
run was orphaned. On resume the implementer read the fresh bead text (the
engine re-reads the bead), found the data layer violated it, fixed it and
added two tests -- editing beads between iterations is a working way to
steer a run, cheaper than a human gate.

## 33. Agents leave junk that lands in the judge's diff -- mitigated

The tests role for `alloy-73m` ran `uv` in the worktree and left a
multi-hundred-line `uv.lock` behind. `WorktreeManager.diff()` stages every
untracked file so the judge sees new code, which means junk goes in too --
and the judge's diff is clipped at 12,000 characters, so a lockfile could
have pushed the real change out of view. Removed by hand and gitignored;
the general fix is a diff that skips ignored *and* obviously generated files
(lockfiles, `.serena/`, `__pycache__`), or a much larger diff budget for the
judge with head/tail clipping per file.

## 34. Second bead: 17 minutes, one iteration -- observed

`alloy-73m` (snapshot assembly) went context 52 s, tests 451 s, codex 3 s
(out of credits, clear message this time), Fable 275 s, verify, Jev `done`
p=0.92 -- 17 minutes wall, about $3.40 in Claude spend. The operator
integration step took another two minutes. The loop is now the shape it was
designed to be; the remaining cost is the full-suite verify at ~4.5 minutes
per iteration (entry 22).

## 35. The plan said package, the implementer shipped a module -- observed

Component 3's design puts the monitor in `src/alloy/monitor/` (`snapshot.py`,
`render.py`, `app.py`); bead 2's implementer created `src/alloy/monitor.py`
instead, and the judge accepted it because no acceptance criterion named the
layout. Bead 3's tests then import `alloy.monitor.render`, so its implementer
first has to convert the module into a package. Harmless here, but a reminder
that anything a later bead depends on must be an acceptance criterion, not a
design note -- the judge only enforces the former.

## 36. Third bead: the Textual view, 15 minutes -- observed

`alloy-44s` went the same way as 73m: one iteration, 206 tests, Jev p=0.89,
about $2.40. The implementer converted the flat module into the package the
plan wanted (entry 35) without being told. Headless smoke test through
Textual's pilot passed on the first try; the interactive view itself has not
yet been watched by a human on a real terminal -- that is the one acceptance
step Alloy cannot perform.

## 37. Fourth bead in 9 minutes; the loop is now routine -- observed

`alloy-c5v.1` (detail pane and judge panel): context 45 s, tests 157 s,
codex 3 s (out of credits), Fable 107 s, verify, Jev p=0.89, 222 tests. The
tests role appended 71 lines to an existing test file, additive only. Four
monitor beads have now gone through Alloy end to end; every one needed the
Codex-to-Fable fallback and none needed a human gate or a second iteration
after the first bead. Total Claude spend for the four beads is roughly $12.

## 38. A session limit on the tests role failed the bead -- fixed

`alloy-c5v.2`'s tests call (Claude Sonnet) ended with "You've hit your
session limit · resets 1:20am". The graph treated any tests-role failure as
`abort`, so a 35-minute wait became a `failed` bead with a finished
checkpoint that `alloy resume` could not restart. Fixed: a failed tests role
now parks the run at the human gate with `resume_to = "tests"`, and
`human_gate` routes the resumed run back to the tests stage instead of the
implementer. For this run the checkpoint was already terminal, so the bead
was reset to `open` by hand and relaunched after the reset time. The same
session limit applies to every Claude model on the account, so a Claude
fallback for this role would not have helped; Codex was out of credits.

## 39. The change summary is whatever the agent said last -- open

`AgentResult.text` for Claude is the harness's final message. On
`alloy-c5v.2` the implementer had started a background wait loop inside its
own session, and its last message was a reply to that loop's completion
("That notification is just the wait loop I started earlier finishing…")
rather than the requested summary paragraph. The judge and the attempt
history got that sentence as the change summary. Cheap mitigation: ask for
the summary inside a fenced marker (e.g. `<summary>…</summary>`) and extract
it from the whole transcript rather than trusting the last message.

## 40. Fifth bead and the epic is done -- observed

`alloy-c5v.2` (harness pid on `inflight_calls`) ran on the default
`tdd-loop` recipe: after the session-limit restart, context 70 s, tests 64 s
(it adopted its own earlier partial edits), Fable 344 s, verify, and the
Claude Sonnet judge said `done` in 39 s with a two-paragraph reason that
named the risks it had checked -- the first bead in this series judged by
prose rather than a distribution. 230 tests. All five children of the
monitor epic have now been implemented by Alloy and merged; the interactive
view has not yet been seen by a human on a real terminal.

## 23. The tests role can be wrong and only the implementer notices -- open

Claude wrote a test asserting the checkpoint stage after a human gate is
`waiting-human`; it is `guard` (the ledger says `waiting-human`, the graph
does not). Codex corrected the assertion and said so, as the prompt allows.
The judge should be told explicitly when tests were edited; today it can
only infer it from the diff.

## 41. The fixed test command is gone (2026-09-23) -- fixed

Alloy used to resolve one test command per run (recipe `verify.command`, then
the context role's `test_command`, then autodetection) and treat that command
as verification. Bead `alloy-21u.7` removed the last of that path:
`ContextPacket.test_command`, `resolve_command`, `run_tests`, the `TestReport`
alias, `VerifySpec` / `RecipeConfig.verify` and `RunContext.verify`.
Verification is now only the verifier loop -- the verifier names a check, Alloy
runs it through `run_check`, and the `verification:` block caps how many checks
may run and for how long. What survives of the old inputs are hints: the
context role may return `check_hints`, the bead's `alloy_test_cmd` is an
operator hint (`Bead.check_hint`), and autodetection from the project layout
fills in when the context role suggested nothing. All of them reach the
verifier under "Hints from the repository (not yet verified)" and none is run
unasked. A legacy `verify:` block still parses and warns; bug beads Alloy
files no longer inherit `alloy_test_cmd`.
