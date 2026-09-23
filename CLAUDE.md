# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:1105d646 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->


## Build & Test

_Add your build and test commands here_

```bash
# Example:
# npm install
# npm test
```

## Architecture Overview

_Add a brief overview of your project architecture_

Verification is a check loop, not a fixed test command. A read-only `verifier`
agent names one check at a time (the tests written for the bead, the wider
suite, lint, typecheck, build or any project script), Alloy runs it in a
subprocess and reads the exit code, and the `judge` step only says `done` when
the required checks are green. The context role may supply `check_hints`, the
optional `alloy_test_cmd` bead metadata is one more hint, and autodetection
(e.g. `cargo test` next to a `Cargo.toml`) fills in when neither gives any;
the verifier sees them under "Hints from the repository (not yet verified)"
and decides what to run. Alloy never runs a hint on its own.

The recipe's `complexity:` block defines `routing: shadow` (estimate without
changing runners) or `routing: live` (select a tier for roles with `tiered: true`),
ordered `tiers:` chains for `simple`, `medium`, and `complex`, optional per-entry
`effort:` (`claude --effort` or Codex `model_reasoning_effort`), and
`escalate_after_retries:` for moving up a tier after repeated retries in live mode.
`alloy_complexity` supplies the operator override; `alloy_complexity_estimated`
records Alloy's estimate or escalation. `alloy recipes` shows routing, escalation,
tier chains, effort, and runner availability. Use `--probe` to smoke-test each
distinct runner/model/effort entry with a two-minute timeout before relying on
model aliases or effort flags; any failed probe gives a non-zero exit, and
transcripts go under Alloy's logs without run-ledger entries.
`alloy status <bead-id> --json` includes `complexity`, `complexity_source`, and
`dispatch_tier` (null in shadow mode).

```bash
bd update <id> --set-metadata alloy_complexity=complex
alloy recipes --probe                     # probe all recipes' distinct tier entries
alloy recipes --probe --recipe tdd-loop --json
```

## Conventions & Patterns

_Add your project-specific conventions here_

- **Monitor UI may use Nerd Font glyphs** (the owner's terminal has them). Read `docs/plans/monitor-nerd-fonts.md` before touching `src/alloy/monitor/`; icons need `nerd|unicode|ascii` fallbacks, and numeric columns are right-aligned.

<!-- alloy:memory:begin -->
reviewed: 2026-09-23
review due: 2026-09-30

### alloy:lesson:jev_runner_must_defer_to_fake_harness
JevRunner.available() and .run() must check an ALLOY_FAKE_CONFIG-style "fake harness mode" flag first and refuse to engage (return unavailable / raise RunnerUnavailable) before checking for a real TYPESAFE_API_KEY.
Why: in alloy-w9d.7, attempt #1's acceptance run hit an unrelated regression in tests/test_scheduler.py::test_recover_adopts_runs_whose_process_died. The cause was that TYPESAFE_API_KEY happened to be set in the test environment, so jev.py's available() returned True and the real HTTP adapter ran instead of the fake CLI fallback the scheduler tests rely on. The fix (attempt #2) added a `_fake_harness_mode()` check gating both available() and run() ahead of the API-key check, restoring the fake-harness ledger path and turning the regression green.
How to apply: any runner that probes real credentials (API keys, tokens) to decide availability should check for the fake-harness env flag (ALLOY_FAKE_CONFIG) first and short-circuit to unavailable, regardless of whether real credentials are present in the ambient environment — otherwise leaked/ambient real credentials silently bypass fake-harness test fixtures and cause spurious, hard-to-diagnose regressions unrelated to the actual change under test.

### alloy:lesson:memory_reads_snapshot_at_run_start
Read project memory (bd memories/lessons) exactly once per run, in initial_state, and stash it in TddState (e.g. memory_keys, memory_lessons) rather than calling ctx.project_memory() again from a later graph step like harvest.
Why: during alloy-4ef.8, adding a second per-run project_memory() read (for harvest's existing_lessons) broke tests/test_memory.py::test_memory_injection because fake bd harnesses assert an exact number of memory-read calls; the fix was to read memory once at run start and pass the cached dict/list through state instead of re-querying.
How to apply: when a new graph step (context, harvest, etc.) needs memory contents, first check TddState for an already-cached field before adding a new ctx.project_memory()/ctx.beads.memories() call; if none exists, add the read to initial_state and thread it through state rather than calling it inline in the step.

### alloy:lesson:monitor_render_color_tag_must_wrap_full_segment
In src/alloy/monitor/render.py, when a threshold color (green/yellow/red) is required to "style" a value, the Textual markup color tag must wrap the entire visible segment (label + percent + bar, e.g. `[{color}]{label} {percent}% {bar}[/]`) as one contiguous tag, not just be present anywhere in the line or wrap an empty/separate span.
Why: in alloy-o89.2, attempt #1 emitted the percent/bar text and the `[{color}][/]` tag as separate pieces, so the hex color string appeared in the line but didn't actually color the percent+bar together. The acceptance judge rejected this as a repair because "styled" means the colored markup must visually apply to the value, not merely co-occur with it. Attempt #2 fixed it by building one `_limits_window_segment()` helper that wraps label+percent+bar in a single `[{color}]...[/]` span, which passed both the targeted tests and the acceptance judge.
How to apply: when implementing or reviewing any "render X in color Y" acceptance criterion in this codebase's Textual-markup rendering code, verify (and write tests that assert) the color tag's open/close brackets bound the actual value text as one substring, not just that the color hex code appears somewhere in the output line.

### alloy:lesson:role_scoped_memory_snapshot_pattern
When a new stored memory key (e.g. alloy:calibration) should be visible to only one role, snapshot its raw body into its own TddState field in initial_state() (alongside memory_block/memory_check_hints) and inject it only into that role's prompt-building function's project layer — never fold it into the shared memory_block that assemble() feeds to every role.
Why: alloy-4ef.12 added CALIBRATION_KEY this way: memory_calibration is snapshotted once in initial_state (respecting [[memory_reads_snapshot_at_run_start]]), then format_calibration() renders it only inside estimate_prompt's project layer via a keyword arg, keeping tests/test_memory.py's exclusion assertions (only meta-review/calibration keys excluded from the generic render()) and tests/test_memory_injection.py's per-role prompt assertions stable.
How to apply: for any future memory key meant for a single role (not the whole team of roles), add it to _MEMORY_EXCLUDED_KEYS in models.py, give it its own memory_<name> TddState field set in initial_state(), write a small format_<name>() pure renderer, and pass the rendered line as an explicit keyword arg into just that role's *_prompt() function — do not add it to the shared memory_block/_project_layer(memory) call used by every other role.

### alloy:lesson:scheduler_dedupe_repeated_warning_with_instance_field
When a scheduler-loop method (e.g. Scheduler.next_task, called every poll/tick) needs to warn about a bad config value (like an unknown alloy:default:recipe name from memory) without spamming logs on every poll, track the last-warned value in a dataclass instance field (e.g. _unknown_default_recipe) and only log when the new value differs from it — reset/update the field each call.
Why: alloy-4ef.14 needed "one log line per encounter" for an unknown default recipe from bd memory, and next_task() runs on every scheduler tick. Storing last-warned state on the Scheduler instance (alongside _default_recipe) let the acceptance test call next_task() twice with the same bad memory value and assert exactly one matching log record via caplog.
How to apply: for any new memory-driven or config-driven warning inside a per-tick/per-poll method, add a small `_last_warned_<thing>` field to the class's dataclass fields (init=False, default=None) rather than reaching for a global rate-limiter or logging.Filter; test it the same way — call the method twice in one test and assert len(matching caplog records) == 1.

### alloy:lesson:shared_logic_extraction_avoids_cli_scheduler_circular_import
When logic needs to be shared between cli.py and scheduler.py (e.g. memory review/embed), extract it into its own new module (memory_schedule.py) rather than having scheduler import from cli or vice versa.
Why: alloy-4ef.19's design notes flagged "Extracting review logic from cli.py risks circular imports if scheduler imports cli directly" as a risk. The implementation resolved it by creating src/alloy/memory_schedule.py holding the pure due/skip decisions (review_due, dirty_instruction_files) and the shared execution steps (review_plan, apply_review, embed_instruction_files, review_run_id), which both cli.py and scheduler.py import; cli.py's own review/embed functions became thin wrappers delegating to memory_schedule.
How to apply: before adding scheduler->cli or cli->scheduler imports for shared functionality, check whether the shared piece can be pulled into a standalone module that both sides depend on instead.
<!-- alloy:memory:end -->
