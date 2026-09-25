# AGENTS.md — driving Alloy

You are an agent that plans and drives work through **Alloy**. You do not need
to read Alloy's source to do this — this file is the whole interface.

## Python environment

Always run this project through its uv-managed venv: `uv run pytest`,
`uv run python ...`, `uv sync` (or an activated `.venv`). Never use system
`python`/`pip`, a bare `.venv/bin/python` without activation, or
`pip install -e .` from inside an Alloy worktree — that repoints the shared
venv's `alloy` install at the worktree.

## The mental model

Two systems, two timescales:

- **Beads (`bd`) owns the project.** Tasks, priorities, dependencies,
  readiness, and which recipe to run. This is where you plan.
- **Alloy (`alloy`) owns execution.** It picks up one READY bead at a time,
  runs it in its own git worktree through a checkpointed agent loop (context →
  write failing tests → implement → verifier-chosen checks → judge), and writes status back
  onto the bead. It does **not** plan, decompose, or prioritize — that's your
  job, done entirely in Beads before you ever call `alloy run`.

**Alloy runs one bead at a time.** A bead must be small enough that a handful
of checks can prove it, and it must finish inside a recipe's limits (default: 5
iterations, 90 minutes, 20 agent calls — see `alloy recipes`). Verification is
a loop, not a fixed command: a read-only `verifier` agent names one check at a
time (the tests written for the bead, the wider suite, lint, typecheck, build
or any project script), Alloy runs it in a subprocess and reads the exit code,
and the verifier stops when the evidence is sufficient. If a task can't be
checked by commands the verifier can run, it isn't a good Alloy bead.

## From a spec to running work

Never hand a whole feature to Alloy as one bead. Decompose first:

1. **Create one epic** for the feature, for tracking only — it never gets an
   `alloy_recipe` and is never passed to `alloy run`:
   ```bash
   bd create --title "Add OAuth login" --type epic --spec-id <doc-id-or-path>
   ```
2. **Create child beads**, one per independently-testable unit of work (one
   migration, one endpoint, one UI wiring, one test suite — not "backend
   work"). Two ways to do this in bulk instead of one at a time:
   ```bash
   bd create -f breakdown.md          # batch-create from a markdown outline
   bd create --graph plan.json        # batch-create a full dependency graph from JSON
   ```
   Or one at a time:
   ```bash
   bd create --title "<unit of work>" --parent <epic-id> \
     --description "<what, from the spec>" \
     --design "<how, if the spec says>" \
     --deps blocked-by:<other-bead-id>
   ```
3. **Wire dependencies** so Beads' readiness graph enforces the order the spec
   implies (Alloy only ever sees unblocked, READY beads):
   ```bash
   bd link <bead-a> blocks <bead-b>
   bd graph                       # sanity-check the shape before running anything
   ```
4. **Tag every leaf bead for Alloy** — this is required, Alloy ignores beads
   without a recipe:
   ```bash
   bd update <bead-id> --set-metadata alloy_recipe=tdd-loop \
                        --acceptance "<one concrete, testable criterion>"
   ```
   Write acceptance criteria as something a test can check, not a paraphrase
   of the spec section. The `judge`/`guard` step in the recipe only accepts
   `done` when verification is objectively green — vague acceptance text just
   burns iterations against the bead's limits.
5. Optionally suggest a check hint. Alloy never runs it by itself; the
   verifier sees it under "Hints from the repository (not yet verified)" next
   to what the context role and project-layout autodetection suggested, and
   decides which checks actually run:
   ```bash
   bd update <bead-id> --set-metadata alloy_test_cmd='pytest -q tests/oauth'
   ```

## Alloy commands

```bash
alloy init [--repo PATH]              # one-time: create ~/.alloy, register Beads statuses
alloy recipes                         # list recipes, roles, runners, limits — check before running
alloy run <bead-id> [--recipe NAME]   # run one bead to done / human-gate / failure
alloy status [<bead-id>] --json       # what's running, stage, iteration count, tests
alloy logs <bead-id>                  # every agent call in the run + transcript paths
alloy resume <bead-id> -m "<message>" # continue a paused/crashed run with guidance
alloy cancel <bead-id>                # stop the run's process; bead returns to ready, worktree kept
alloy land <bead-id>                  # trial-merge, re-verify, merge into landing.target (default main)
alloy start [--poll SECS] [--recipe N]  # scheduler: poll Beads, run READY work, concurrency 1
alloy stop [--now]                    # stop the scheduler after the current task (--now: cancel it too)
alloy monitor [--once [--json]]       # live htop-style view of every active run; --once prints one snapshot
alloy limits [--json]                 # probe installed harness usage limits; refresh limits.json
```

Ctrl-C or SIGTERM on `alloy run` stops the harness with it and leaves the run
resumable (`alloy run <bead-id>` again picks it up). The detached scheduler
logs to `~/.alloy/scheduler.log`.

Roles can name a `fallback` runner in the recipe YAML (see `alloy recipes`):
when the primary is missing, rate-limited or times out, the same prompt goes
to the fallback and both attempts appear in `alloy logs`. The built-in
recipes fall back from Codex to Claude Fable for implementation.

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

`--json` on any command gives machine-readable output — prefer it over parsing
the table output.

## Project memory

Alloy stores what it learns about a repository as Beads memories and reads
them once per run into a `## Project memory` block in every role's prompt.
Memory informs; the guard decides. Nothing remembered can raise a limit,
skip a check or pass a human gate.

Keys starting with `alloy:` are alloy-owned and get a provenance trailer.
Alloy writes `alloy:check-hints` (check commands offered to the verifier),
`alloy:lesson:<key>` (harvested lessons), `alloy:human:<bead-id>` (guidance
saved with `--remember`), `alloy:calibration` (per-tier run statistics, seen
only by the estimator), `alloy:regression:<area>` (role-specific regression
areas), `alloy:default:recipe` (scheduler default), `alloy:meta:*`
(bookkeeping) and `alloy:review:*` (contradictions and proposals). Everything
else is human-owned: write it with `bd remember`, and Alloy leaves it alone.

```bash
alloy memory list                          # inventory with owner, provenance, age and flags
alloy memory review                        # read-only plan; nothing changes
alloy memory review --apply                # apply alloy-owned verdicts
alloy memory embed                         # splice the embed set into AGENTS.md and CLAUDE.md
alloy resume <bead-id> -m "<guidance>" --remember          # store under alloy:human:<bead-id>
alloy resume <bead-id> -m "<guidance>" --remember=<key>    # or a custom key
```

`alloy memory embed` rewrites only the region between the HTML-comment
markers `alloy:memory:begin` and `alloy:memory:end` in each
`memory.instruction_files` entry (default `AGENTS.md`, `CLAUDE.md`), or appends
one when it is missing. It never runs git; commit the result yourself. Do not
hand-edit inside the markers — the next embed overwrites it. Review is due
every 7 days or 20 runs (`review_every_days`, `review_every_runs`); the
scheduler runs `alloy memory review --apply` then `alloy memory embed`, and
skips the embed while an instruction file is dirty.

Prompts are five layers in fixed order — static, project, run, task,
volatile — and `prefix_hash` (sha256 of the first three) is recorded on every
agent call. The `prefix` column of `alloy logs` shows the truncated hash: if
it changes mid-run, something volatile leaked into the cacheable prefix.

## Running a decomposed feature

Either drive it bead-by-bead:
```bash
alloy run <bead-id>          # repeat down the dependency chain, checking status between
```
or start the scheduler once and let it work the whole graph unattended,
picking up each bead as its blockers clear:
```bash
alloy start
```

## Rules to not break

- Never set `alloy_recipe` or run `alloy run` on an epic bead — only on leaf
  work that has its own acceptance criteria, provable by checks the verifier
  can run.
- Don't try to push a bead to `done` yourself. `guard` is the only thing that
  can accept `done`, and it refuses while tests are red. If a run reaches
  `waiting-human`, use `alloy resume <bead-id> -m "..."` with guidance, don't
  edit the bead status directly.
- Standalone beads sit at `review-ready` until landing. Recipes opt in via
  `landing: {mode: off|auto, target: <branch>}`; shipped `tdd-loop` and
  `tdd-loop-jev` set `landing: {mode: auto, target: main}`, so the scheduler
  runs `alloy land` after a successful run. Landing is never set via
  `alloy_recipe` metadata. Epic children share one worktree/branch
  (`alloy/<epic-id>`); children close on success and the epic lands when all
  descendants are closed. `cleanup_worktree_on_success: false` keeps the
  worktree until land removes it. For stacked features, decide up front
  whether later beads' worktrees branch off an earlier bead's branch or off
  main — Alloy does not stack branches for you.
- `.beads/` is a Dolt database with its own version control — do not commit it
  into the project's git repo. If you want a reviewable, git-friendly record
  of issues, enable `bd config set export.auto true` and use the resulting
  `.beads/issues.jsonl`, not the Dolt data itself.
- If `alloy recipes` reports a runner as missing, fix that before running —
  don't route around it by hand.

## Bugs an agent finds

Agents running inside a recipe may hit a defect in existing code that is not
their task. They do not fix it: they append a `<bug>` block (title, where,
evidence, `blocks_task: yes|no`) to their output, and stop only if it blocks
them. Before verification, Alloy hands each untriaged report to the `triage`
role (Jev, falling back to Claude), which reads the task brief, acceptance
criteria, test results, the bugs already filed and the remediations already
performed in this run, plus the project brief in `.alloy/project.md` when one
exists, and returns one of five labels:

| Label | What Alloy does |
|-------|-----------------|
| `not-a-bug` | Nothing filed; the implementer is told the report is part of its task. |
| `duplicate` | Matches a bug already filed in this run; nothing filed. |
| `non-blocking` | Filed at P3 with label `alloy-bug`; the run continues and must not fix it. |
| `blocking` | Filed at P1, `alloy-bug`, pre-claimed, with a generated acceptance criterion; the run routes to `remediate`, which fixes it in its own bead and merges the fix into the parent's branch before the task continues. |
| `needs-human` | Filed at P1 with labels `alloy-bug` and `human`; the running bead is made blocked-by it and the run parks at the human gate. |

Every filed bug bead carries a `discovered-from` dependency on the bead whose
run found it (it does not block that bead) and `alloy_discovered_in_run`
metadata. `bd list --label alloy-bug` shows everything Alloy has filed;
Supervising agents must not poll: stream `alloy events --follow --attention`
(needs-human, failed, stalled; see README "Supervising Alloy without polling")
and act per event. `alloy start --stall-minutes N` sets the stall threshold.

`bd human list` is the review path for `needs-human` bugs -- resolve or
dismiss the bug, then `alloy resume <bead-id>`. `needs-human` is the carve-out
for architecture and product decisions (schema or public API changes,
dependency swaps, behaviour the acceptance criteria contradict), not the
default; there is no cap on how many blocking bugs one run may remediate, the
triage role weighs the remediations so far and the progress made.

A merged remediation shows up on the parent's branch as the parent's own
work-in-progress commit followed by a merge of `alloy/<bug-id>`, with notes on
both beads naming the run; a remediation that could not land leaves the parent
branch clean at its WIP commit and the bug bead `failed` with the reason.

Remediation is bounded twice, never by a counter. Before a child's fix merges,
the `scope` role (Jev) reads the child's diff against the bug bead and the
project context and returns a scope verdict; only `merge` lands the fix, while
`too-broad`, `off-target` or a merge conflict fails the child and parks the
parent at the human gate with the reason. Each run's own budget is the
deterministic backstop: a run is capped by its own `max_agent_calls`
(remediation children spend their own budget, not the parent's), while wall
time spent in children still counts toward the parent's
`max_wall_time_minutes`, so a bead can only remediate as much as its own
limits allow. Remediation is one level
deep -- a blocking bug found inside a child is filed unclaimed at P1 and the
child parks, which parks the parent in turn. When reviewing a parent branch
that carries a merged child branch, read the parent's WIP commit, the merge of
`alloy/<bug-id>`, and the parent's later commits as one unit: the parent's own
verify and judge ran again after the merge, so a fix that broke the parent
would have been caught there, and the run's `remediations` list (bead id,
child run id, outcome, reason) says which fixes landed and which did not.

## Agent skills

Decomposition and day-to-day Alloy operation are spelled out in project skills
(kept in sync between editors):

| Skill | Role |
|-------|------|
| `alloy-product-designer` | Spec → epic + dependency-ordered beads |
| `alloy-manager` | Run, monitor, human gates, review, merge |

- **Cursor:** `.cursor/skills/<name>/SKILL.md`
- **Claude Code:** `.claude/skills/<name>/SKILL.md`

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:46cd31e7 -->
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
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

<!-- BEGIN BEADS CODEX SETUP: generated by bd setup codex -->
## Beads Issue Tracker

Use Beads (`bd`) for durable task tracking in repositories that include it. Use the `beads` skill at `.agents/skills/beads/SKILL.md` (project install) or `~/.agents/skills/beads/SKILL.md` (global install) for Beads workflow guidance, then use the `bd` CLI for issue operations.

### Quick Reference

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Claim work
bd close <id>           # Complete work
bd prime                # Refresh Beads context
```

### Rules

- Use `bd` for all task tracking; do not create markdown TODO lists.
- Run `bd prime` when Beads context is missing or stale. Codex 0.129.0+ can load Beads context automatically through native hooks; use `/hooks` to inspect or toggle them.
- Keep persistent project memory in Beads via `bd remember`; do not create ad hoc memory files.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.
<!-- END BEADS CODEX SETUP -->

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
