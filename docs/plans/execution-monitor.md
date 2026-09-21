# Execution Monitor — architecture & implementation plan

Status: revised after review by codex/Astra (read-only review, 2026-09-21).
This revision replaces the v1 draft; see git history for what changed and why
if needed.

## Why

Alloy currently answers "what is happening?" with `alloy status` (a static
table, one poll) and `alloy logs <bead>` (a per-run agent-call ledger).
Neither refreshes live, neither shows what an agent is doing *right now*, and
neither aggregates token spend across a run. An operator needs an htop-style
view: one screen, refreshing continuously, showing every active run's linear
progress, which agent is currently executing under it, what it is costing,
and which runs are thrashing (retrying, escalating to consilium, or stuck
waiting on a human).

Concurrency note, corrected from the v1 draft: `Scheduler.tick()`
(`src/alloy/scheduler.py:69`) awaits one whole `engine.run()` call before
returning, so today's scheduler is strictly sequential across *beads*
regardless of the `concurrency` field's value — that field is not yet wired
to overlapping runs. Overlapping agent calls *do* already happen today, but
within a single run: consilium fans out several critics concurrently under
one shared `run_id` via `Send("critic", ...)`
(`src/alloy/recipes/tdd_loop.py:531-551`). The data layer must handle that
case correctly now, not as a future concern.

This also gives us a second, more demanding test of the `jev` runner
(`src/alloy/runners/jev.py`), used today only as an optional judge in
`tdd-loop-jev.yaml`: the monitor surfaces the judge's raw `confidence` value
per run so a Jev-judged run's calibrated probability and a Claude-judged
run's self-reported number can be compared side by side while both actually
execute. Getting this right requires distinguishing the judge's *raw*
verdict from what `guard()` did with it afterward (see Component 1, item 3).

## Design

### Data flow

Execution state already lives in two local SQLite files under `~/.alloy`
(`alloy.db` via `Store`, `workflows.db` via LangGraph's checkpointer), both
in WAL mode, both already read while a run is in progress by `alloy status`.
The monitor is a poller of those same files on a timer — no new client/server
protocol, no daemon the scheduler must run.

Scope of "read-only": the `alloy monitor` command itself never calls a
mutating method on `Store`, `BeadsClient`, or the checkpointer in response to
anything the operator does at the keyboard — no keypress cancels, claims, or
resumes a run. This is a guarantee about the monitor's *own* actions, not a
claim that nothing it touches ever writes: `Store`'s constructor still runs
idempotent schema DDL the first time a given `alloy.db` is opened (exactly
what `alloy status` already does today), and `inflight_calls` reconciliation
(Component 1, item 2) is a deliberate write, but it is performed by the
*data layer* as part of Alloy's existing crash-recovery path, and is invoked
from the scheduler/engine, never from the monitor process.

One real gap exists today: `Store.record_agent_call` only inserts a row
*after* a harness process exits (`src/alloy/runtime.py:56-74`), so there is
no record of a call still in flight. The monitor's "currently running agent"
panel needs exactly that, so the data layer adds it.

### Component 1 — data layer (`alloy.store`)

**1. In-flight call tracking, keyed by invocation, not by run.**

```sql
CREATE TABLE IF NOT EXISTS inflight_calls (
    call_id     TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    bead_id     TEXT NOT NULL,
    role        TEXT NOT NULL,
    runner      TEXT NOT NULL,
    model       TEXT,
    started_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS inflight_calls_run_idx ON inflight_calls(run_id);
```

A run can have more than one call in flight at once (consilium's critic
fan-out shares one `run_id` across concurrent `ctx.call()` invocations —
`tdd_loop.py:553-572`), so `run_id` cannot be the primary key. `RunContext.call`
(`src/alloy/runtime.py:45-74`) generates `call_id = uuid.uuid4().hex` at the
top of the method, inserts the row immediately before `await runner.run(...)`,
and removes it by `call_id` (never by `run_id`) in a `finally:` around the
whole call body, so the row is gone whether the call succeeds, returns a
handled `RunnerUnavailable`, or the awaiting task is cancelled
(`asyncio.CancelledError` still runs `finally:` blocks). `Store.active_calls(run_id: str | None = None) -> list[dict]`
returns current rows, optionally filtered to one run.

To close the gap Codex flagged — a separate insert-to-`agent_calls` and
delete-from-`inflight_calls` would leave a polling window where a just-finished
call is invisible in both tables — completion is one transaction:
`Store.finish_call(call_id, *, run_id, bead_id, role, iteration, result)`
opens one connection and, in it, both deletes the `inflight_calls` row and
inserts the `agent_calls` row (the same insert `record_agent_call` does
today; rename/replace that method so there is exactly one write path for a
finished call). `RunContext.call` calls this once, after `runner.run()`
returns, instead of calling `record_agent_call` directly.

**2. Reconciliation ownership — explicit, not implied.**

`Store.reconcile_inflight() -> list[dict]` deletes any `inflight_calls` row
whose `run_id` maps to a `runs.pid` that is no longer alive (same liveness
check `orphaned_runs()` already uses, `store.py:176-179`), returning what it
removed. It is called from exactly two places, both already responsible for
reconciling crash state, and from nowhere else:

- `Scheduler.recover()` (`scheduler.py:55-67`), before it adopts orphaned
  runs — reconcile first, so a stale in-flight row from the crashed process
  never gets attributed to whatever runs next.
- `Engine._execute()`, immediately *before* it overwrites `runs.pid` with the
  current process's pid on a resume (`engine.py:190-191`) — this ordering
  matters: once the row's owning `run_id` has a live pid again (the new
  process), the dead-pid liveness check can no longer tell a genuinely-leaked
  row from a fresh one, so it must run first.

The monitor process itself never calls `reconcile_inflight()`.

**3. The judge's raw verdict, separate from what `guard()` did with it.**

`guard()` in `tdd_loop.py:458-517` can rewrite the judge's decision --
downgrading `done` to `retry` when tests are red (`tdd_loop.py:473-479`),
downgrading `consilium` to `retry` when the consilium budget is spent
(`tdd_loop.py:497-508`) -- and a human resume replaces the decision again
(`tdd_loop.py:619` area). The graph-state `decision` field the monitor could
otherwise read is therefore the *effective* decision, not necessarily what
the judge (Jev or Claude) actually returned, and its `confidence` can be an
Alloy-constructed default (e.g. `0.0` from the `guard`-built
`JudgeDecision(...)` calls) rather than a reported value.

Fix: add a nullable `structured_json TEXT` column to `agent_calls`, populated
from `result.structured` (as JSON, or `NULL` when absent) in
`Store.finish_call`. The monitor's judge panel reads the most recent
`agent_calls` row with `role = 'judge'` for the run and shows its
`structured_json` labelled as "judge said" (raw), next to the current
graph-state `decision` labelled "Alloy did" (effective) whenever they
differ. If a judge call was not `ok` or had no structured output at all, the
panel says exactly that -- "no parseable verdict" -- rather than showing a
`0.0` confidence indistinguishable from a genuine low-confidence answer.

**4. Usage normalization -- specified now, not left to the implementer.**

Every runner's `usage` dict is whatever that CLI happened to emit, passed
through largely as-is (`runners/claude.py:44-47`, `runners/cursor.py`,
`runners/codex.py`, `runners/jev.py`). Observed shapes in this codebase
today: `input_tokens`/`output_tokens` (jev, the common case), Claude's own
envelope plus injected `total_cost_usd`/`num_turns`, and the fake Pi harness
used in tests emits three different shapes across its scripted scenarios
(`tests/fakebin/_fake.py:91,105,112,116`): snake_case, `camelCase`
(`inputTokens`/`outputTokens`), and a totals-only `{"input_tokens": 100}`
with no output count at all.

Add `alloy.usage.normalize(raw: dict) -> dict` returning exactly
`{"input_tokens": int | None, "output_tokens": int | None, "total_tokens": int | None}`:

- Recognize `input_tokens`/`output_tokens` and `inputTokens`/`outputTokens`
  (case variants seen above) and OpenAI-style `prompt_tokens`/
  `completion_tokens`, in that preference order.
- If both input and output are known, `total_tokens` is their sum.
- If only one total-like number is present and nothing distinguishes
  input from output, set `total_tokens` to it and leave input/output `None`
  -- never guess a split.
- An empty or missing usage dict returns all three fields as `None` (the
  aggregation layer treats `None` as 0 for summation, but the normalized
  record itself must be able to say "unreported" instead of silently
  reporting a false zero).
- Unknown fields (`total_cost_usd`, `num_turns`, `session_id`, ...) are
  ignored for token totals. `total_cost_usd`, when present, is carried
  through as a separate `cost_usd: float | None` field on the same
  normalized record -- this plan's cost promise is "sum what harnesses
  already report," not currency conversion or per-token pricing tables.

`Store.token_totals(run_id) -> dict` and `Store.token_totals_by_role(run_id)
-> dict[str, dict]` sum normalized records from `agent_calls.usage_json`
(each summed field is `0` if every contributing record was `None` for that
field, so a run with genuinely no usage data returns zeros, matching the
already-agreed acceptance criterion, without conflating "reported zero" and
"never reported").

**5. Repo-scoped and unbounded lifetime queries.**

`Store.active_runs()` and `Store.all_runs()` currently query across the
whole `alloy.db`, with no `repo` filter (`store.py:161-174`) -- every other
Alloy surface (`BeadsClient`, `alloy status`, `alloy logs`) is implicitly
scoped to one repository via `--repo`. Add an optional `repo: Path | None`
parameter to both, filtering on the existing `runs.repo` column, and use it
from the monitor (always passing `engine.repo`, exactly like every other CLI
command's `_engine(repo, root)`). A multi-repo view is explicitly out of
scope.

For lifetime done/failed totals, do not sum `all_runs(limit=100)` (a
default-100-row window, not a total). Add
`Store.run_status_totals(repo: Path) -> dict[str, int]`, an unbounded
`SELECT status, COUNT(*) FROM runs WHERE repo = ? GROUP BY status` query.

### Component 2 — snapshot assembly (`alloy monitor --once --json`)

A pure, synchronous function, `build_snapshot(engine: Engine) -> dict`,
assembling one point-in-time view from the data layer. This is its own bead
(see Scope below) because it is independently testable without any terminal
or keyboard concerns, and because it is the acceptance-testable surface for
the whole feature: a `rich.live.Live` loop cannot be asserted against in
pytest, but the dict this function returns can be, directly, with no
terminal involved.

For each row in `Store.active_runs(repo=engine.repo)`:
- Look up that *specific run's* checkpoint via its own `thread_id`
  (`record["thread_id"]`, already on the `runs` row) with
  `read_checkpoint(paths.workflows_db, thread_id)` -- **not**
  `Engine.graph_snapshot(bead_id)`, which resolves to the *latest* run for
  that bead and would silently show a different run's state for a bead with
  more than one recorded run (`engine.py:285-290`). Add
  `Engine.graph_snapshot_for_run(run_id: str) -> dict | None` (looks up
  `thread_id` via `store.get_run(run_id)`, then calls `read_checkpoint`) and
  use that instead; keep `graph_snapshot(bead_id)` for its existing callers
  unchanged.
- Effective iteration/consilium limits: `max_iterations = recipe.limits.max_iterations
  * budget`, `max_consiliums = recipe.limits.max_consiliums * budget`, where
  `budget = 1 + checkpoint_state.get("budget_extensions", 0)` -- the exact
  formula `RunContext.budget()` already uses (`runtime.py:107-113`). Read
  `budget_extensions` from the checkpoint, not from anywhere else.
- `consiliums` count: read from the checkpoint's `consiliums` graph-state
  field, not from `runs.consiliums`. The "consilium produced no usable
  opinions" branch (`tdd_loop.py:574-584`) updates graph state but does not
  call `ctx.set_consiliums()`, so `runs.consiliums` can undercount; graph
  state is incremented on every path and is authoritative.
- `current_calls`: `Store.active_calls(run_id)` -- a list (zero, one, or
  several entries), each with role/runner/model/`elapsed_seconds` (`now -
  started_at`).
- `tokens`: `Store.token_totals(run_id)`.
- `judge`: the raw-vs-effective pair from Component 1 item 3, or `null` if
  the run has not reached the judge stage yet.
- `requested_vs_effective`: for `current_calls` and the most recent
  `agent_calls` row, show both the recipe's configured `spec.runner`/
  `spec.model` (from `recipe.role(role_name)`) and the actual
  `result.runner`/`result.model` the ledger recorded -- these can differ via
  the `astra` -> `codex` alias or a runner's own `default_model`
  (`runners/__init__.py:32,49`, `runners/base.py:167`). Label them
  distinctly (`requested_runner`/`effective_runner`, etc.) rather than
  picking one.

Header fields: scheduler status (`read_pid(paths.scheduler_pid)`), a ready
count from `beads.ready(limit=1000)` labelled `"ready_capped_at": 1000`
(honest about the cap rather than presenting it as an exact total -- `bd
ready`'s own default is 50, `beads.py:134-143`), and
`Store.run_status_totals(repo)` for lifetime done/failed/cancelled counts.

**Frozen JSON shape** (`alloy monitor --once --json` output; every field
listed here must be present, `null` where a value is genuinely absent, never
omitted):

```json
{
  "root": "/home/vader/.alloy",
  "repo": "/home/vader/MY_SRC/alloy",
  "scheduler": {"running": true, "pid": 12345},
  "ready_count": 3,
  "ready_capped_at": 1000,
  "lifetime": {"done": 12, "failed": 2, "cancelled": 1},
  "runs": [
    {
      "bead_id": "alloy-a1b2",
      "run_id": "0d9f...",
      "recipe": "tdd-loop-jev",
      "status": "running",
      "stage": "implement",
      "iteration": 2,
      "max_iterations": 5,
      "consiliums": 0,
      "max_consiliums": 1,
      "tests_summary": "3 passed, 1 failed",
      "elapsed_minutes": 7,
      "current_calls": [
        {"role": "implement", "requested_runner": "astra", "effective_runner": "codex",
         "requested_model": null, "effective_model": null, "elapsed_seconds": 42.1}
      ],
      "tokens": {"input_tokens": 8123, "output_tokens": 512, "total_tokens": 8635,
                 "cost_usd": null},
      "judge": {
        "raw": {"decision": "retry", "confidence": 0.61},
        "effective": {"decision": "retry", "reason": "a specific fix remains"},
        "matches_effective": true
      },
      "worktree": "/home/vader/.alloy/worktrees/alloy-a1b2",
      "branch": "alloy/alloy-a1b2"
    }
  ]
}
```

A run with no in-flight call has `"current_calls": []` (not `null`). A run
that has not reached the judge stage has `"judge": null`. A run with no
recorded usage has `"tokens": {"input_tokens": 0, "output_tokens": 0,
"total_tokens": 0, "cost_usd": null}`.

### Component 3 — interactive `alloy monitor` view

Built on `rich.live.Live` + `rich.layout.Layout`, calling `build_snapshot`
(Component 2) on a timer, `--interval` seconds apart (default 1.0), default
mode with no flags. This bead depends on Component 2 existing and being
correct; it adds no new data-layer logic, only rendering and input.

**Terminal/input mechanism, specified so the implementing agent does not have
to invent one:** `rich` renders; it does not read keys. Use the stdlib
(`termios`/`tty` on POSIX; this project's dev and CI environment is Linux --
Windows is explicitly out of scope for the interactive view, `--once --json`
remains fully cross-platform since it does no raw terminal I/O) to put stdin
into cbreak mode for the duration of the session, restored in a `finally:`
that also runs on `KeyboardInterrupt`/`SystemExit`/any exception, so a crash
never leaves the operator's shell in raw mode. Read keys via a background
thread (or `asyncio`'s `loop.add_reader` on stdin's fd) pushing into a small
queue the render loop drains -- never call a blocking read directly in the
same loop that also has to redraw every `--interval` seconds.

**Polling must never block on the keyboard, and vice versa.** `BeadsClient`
calls are synchronous subprocess calls with up to a 60s timeout
(`beads.py:89`), and SQLite connections allow a 30s busy-wait
(`store.py:89-93`); if `build_snapshot` ran directly in the same loop that
also has to notice `q`, a single slow poll would make the whole program
briefly unresponsive to quitting. Run each snapshot refresh via
`asyncio.to_thread(build_snapshot, engine)` (or an equivalent background
thread) so the keyboard-reading loop can always process `q` immediately
regardless of what a poll is doing; show the *previous* snapshot with a
"refreshing..." indicator while a new one is in flight rather than blocking
the redraw on it.

Controls, deliberately limited to navigation: up/down (or `j`/`k`) moves the
selected row, `enter`/`l` toggles the detail pane, `q` quits. No keybinding
may call anything on `Store`, `BeadsClient`, or the checkpointer beyond what
`build_snapshot` already reads -- verify this by construction (the keymap
dispatch table only contains handlers that touch local view state) and by a
test asserting that fact about the dispatch table, not by convention alone.

Resize, a selected row disappearing (its run finished between polls), zero
active runs, and a completely fresh `~/.alloy` with no prior runs must all
render without raising -- covered in acceptance criteria below.

## Multi-bead execution process (for whoever runs this plan through Alloy)

Per `AGENTS.md`'s existing rule ("decide up front whether later beads'
worktrees should branch off an earlier bead's branch or off main -- Alloy
does not stack branches for you"): each bead below gets its own worktree
branched from `main` (`worktree.py:57`'s default), and lands at
`review-ready`, not merged. Because bead 2 imports code bead 1 adds to
`Store`, and bead 3 imports the function bead 2 adds, **bead 1's branch must
be merged into `main` before bead 2 is started, and bead 2's branch merged
into `main` before bead 3 is started.** This is a manual integration step the
operator performs between runs; Alloy's own dependency tracking (a `blocked-by`
link) only prevents Beads from offering the next bead as *ready* early -- it
does not merge branches. This plan's own spec file
(`docs/plans/execution-monitor.md`, committed to `main`) is available in
every bead's worktree regardless of merge order, since it is repo content,
not generated code.

Each bead's Design section below is self-contained for what it must build and
how "done" is judged; it points at this file for the full shared rationale
(schema details, the frozen JSON shape, the normalization spec) since the
document is committed and readable from inside any bead's worktree, but does
not depend on the reader having *this* narrative context already loaded --
re-read the relevant Component section here before implementing.

## Scope for this pass

Three beads, in dependency order (data layer -> snapshot -> interactive view):

1. **Data layer** -- Component 1 in full.
2. **Snapshot assembly & `alloy monitor --once --json`** -- Component 2 in
   full. Depends on (1).
3. **Interactive `alloy monitor` view** -- Component 3 in full. Depends on (2).

## Deferred / explicitly out of scope

- A networked monitor (connecting to a remote `alloy start` over a socket
  instead of polling local SQLite) -- revisit only if the scheduler and the
  operator's terminal are ever on different machines.
- Throughput/success-rate rollups over time (runs/hour, mean
  iterations-to-done) -- needs a time-bucketed query this plan does not
  specify; a good v2 candidate once v1's data layer exists.
- Any write-capable "control mode" (cancel/resume from a keypress) -- a
  plausible v2, gated behind an explicit flag and confirmation prompt.
- Windows support for the interactive view (`--once --json` is unaffected).
- Per-token cost estimation beyond passing through a harness-reported
  `total_cost_usd` verbatim.

## Alloy monitor: data layer

Add invocation-level in-flight call tracking, reconciliation, raw judge
verdict capture, usage normalization, and repo-scoped/lifetime aggregate
queries to `alloy.store`. No terminal UI, no CLI command in this bead.

### Description

Full rationale and exact schema/behavior: see "Component 1 -- data layer" in
`docs/plans/execution-monitor.md` (committed at the root of this repository
-- read it before writing any code; the summary below is not a substitute
for it).

### Design

- Add the `inflight_calls` table (schema in the plan doc) with `call_id` as
  its primary key, indexed by `run_id`. A run may have more than one row at
  once (consilium's concurrent critics share a `run_id`); do not assume at
  most one.
- Change `RunContext.call` (`src/alloy/runtime.py`) to generate a `call_id`,
  insert an `inflight_calls` row before awaiting the runner, and call a new
  `Store.finish_call(call_id, ...)` after -- one method, one transaction,
  that both deletes the `inflight_calls` row and inserts the `agent_calls`
  row (replacing today's separate `record_agent_call`). Use `finally:` so
  the in-flight row is removed on every exit path, including
  `asyncio.CancelledError`.
- Add `Store.reconcile_inflight()`, called only from `Scheduler.recover()`
  and from `Engine._execute()` immediately before it reassigns `runs.pid` on
  a resume -- not from anywhere else, and specifically not from any new
  monitor code.
- Add a nullable `structured_json` column to `agent_calls`, populated from
  `result.structured` in `Store.finish_call`.
- Add `alloy/usage.py` with `normalize(raw: dict) -> dict` per the plan
  doc's exact field list and precedence order, plus `Store.token_totals(run_id)`
  and `Store.token_totals_by_role(run_id)` built on it.
- Add an optional `repo: Path | None` filter to `Store.active_runs()` and
  `Store.all_runs()`, and add `Store.run_status_totals(repo: Path) ->
  dict[str, int]` (unbounded group-by-status count).
- Add `Engine.graph_snapshot_for_run(run_id: str) -> dict | None` in
  `src/alloy/engine.py` (look up `thread_id` via `store.get_run(run_id)`,
  then `read_checkpoint`), leaving the existing `graph_snapshot(bead_id)`
  untouched for its current callers.

### Acceptance Criteria

- Two concurrent `RunContext.call()` invocations sharing one `run_id` (as
  consilium's critic fan-out produces) both appear in `Store.active_calls()`
  simultaneously with distinct `call_id`s; finishing one leaves the other
  still present.
- A call that raises inside `runner.run()` (including a raised
  `asyncio.CancelledError`) leaves no row behind in `inflight_calls` once the
  exception has propagated out of `RunContext.call`.
- `Store.reconcile_inflight()` removes rows whose run's `runs.pid` is dead
  and leaves rows for a run with a live `pid` untouched; it is not called
  anywhere except `Scheduler.recover()` and the resume path in
  `Engine._execute()` (verify by reading the diff, not just by test).
- After `Store.finish_call(...)`, the `inflight_calls` row for that
  `call_id` and the new `agent_calls` row are never both absent at the same
  time from any reader's point of view (i.e. they change in one
  transaction) -- test this by asserting the method's SQL executes both
  statements over one connection/commit, not by timing.
- `agent_calls.structured_json` round-trips a judge's structured decision
  (including its `confidence`) and is `NULL` when a call had no structured
  output.
- `normalize()` handles, at minimum, each shape actually observed in this
  codebase today: `input_tokens`/`output_tokens`, `inputTokens`/
  `outputTokens`, a totals-only `{"input_tokens": N}` with no output key,
  and an empty dict -- producing the exact `{"input_tokens", "output_tokens",
  "total_tokens", "cost_usd"}` shape from the plan doc in every case, with
  `None` (not `0`) for anything genuinely unreported.
- `Store.token_totals`/`token_totals_by_role`, given a run with several
  `agent_calls` rows across at least two roles including at least one with
  only a totals-only usage shape, return correct sums with unreported
  fields treated as `0` in the sum.
- `Store.active_runs(repo=...)` and `Store.all_runs(repo=...)` only return
  rows for the given repo; `Store.run_status_totals(repo)` returns exact
  counts across the whole table, not limited to any default row cap.
- `Engine.graph_snapshot_for_run(run_id)` returns the checkpoint for that
  specific run even when the same bead has more than one recorded run (test
  this directly -- create two runs for one bead and assert each run's
  snapshot is its own, not the latest one).
- All new code is covered by tests under `tests/`, following this repo's
  existing rule that no test calls a real model or a real CLI harness.
- `python -m pytest -q` passes for the whole repo, not just the new tests.

### Priority

1

### Type

feature

### Labels

alloy, monitor

## Alloy monitor: snapshot assembly and --once --json

Add `build_snapshot(engine) -> dict` and the `alloy monitor --once --json`
non-interactive command, producing the frozen JSON shape from the plan.
Depends on the data-layer bead (its `Store`/`Engine` additions must already
be merged to `main`).

### Description

Full rationale, the exact frozen JSON example, and every field's source
query: see "Component 2 -- snapshot assembly" in
`docs/plans/execution-monitor.md` (committed at the root of this repository
-- read it, including the worked JSON example, before writing any code).

### Design

- Add `build_snapshot(engine: Engine) -> dict` (a plain function, e.g. in a
  new `alloy/monitor.py`), assembling exactly the fields and using exactly
  the sources listed under "Component 2" in the plan doc: per-run checkpoint
  via `Engine.graph_snapshot_for_run(run_id)` (never `graph_snapshot(bead_id)`),
  the `budget_extensions`-adjusted effective limits, `consiliums` from
  checkpoint state (not `runs.consiliums`), `Store.active_calls(run_id)`,
  `Store.token_totals(run_id)`, the raw-vs-effective judge pair from the
  most recent judge-role `agent_calls` row, and requested-vs-effective
  runner/model for `current_calls` and the latest completed call.
- Add the `monitor` Typer command in `src/alloy/cli.py`: `--once` plus
  `--json` together print one `build_snapshot(engine)` result as JSON and
  exit 0; neither flag alone is this bead's concern (the interactive default
  is the next bead). Reuse `_engine(repo, root)` exactly like every other
  command.
- Match the frozen JSON shape in the plan doc field-for-field, including the
  `null`-vs-empty-list-vs-zero rules it specifies (`current_calls: []` when
  none, `judge: null` before the judge stage, `tokens` all-zero-not-null
  when unreported).

### Acceptance Criteria

- `alloy monitor --once --json` exits 0 and prints one JSON object matching
  the frozen shape in the plan doc field-for-field (write a test that
  asserts on the full key set at both the top level and one `runs[]` entry,
  not just "at least" a few fields).
- With zero active runs, `"runs"` is `[]` and every header field is still
  present and correctly typed (`ready_count` an int, `lifetime` an object
  with `done`/`failed`/`cancelled` keys, `scheduler.running` a bool).
- A run with no in-flight call has `"current_calls": []`; a run that has not
  reached the judge stage has `"judge": null`; a run with no usage data has
  every `tokens` field present and zero (or `null` for `cost_usd`
  specifically), not omitted.
- A bead with two recorded runs shows each run's own judge/iteration state
  in its own entry -- not both entries showing the latest run's state.
- Running against a freshly-initialized `~/.alloy` with no prior runs at all
  does not raise.
- `python -m pytest -q` passes for the whole repo.

### Priority

1

### Type

feature

### Labels

alloy, monitor

## Alloy monitor: interactive live view

Add the default (no `--once`) `alloy monitor` interactive dashboard:
`rich`-rendered, refreshed on a timer, read-only keyboard navigation.
Depends on the snapshot-assembly bead (its `build_snapshot` and CLI scaffold
must already be merged to `main`).

### Description

Full rationale, panel layout, and the terminal/input/threading requirements:
see "Component 3 -- interactive `alloy monitor` view" in
`docs/plans/execution-monitor.md` (committed at the root of this repository
-- read it before writing any code; the terminal-lifecycle and
polling-vs-input-threading requirements there are not optional details).

### Design

- Extend the `monitor` command (added in the previous bead) so that, absent
  `--once`, it runs the interactive loop: `rich.live.Live` +
  `rich.layout.Layout`, refreshed every `--interval` seconds (default 1.0),
  each refresh calling `build_snapshot(engine)` off the input-handling
  thread (e.g. via `asyncio.to_thread`) so a slow Beads/SQLite call never
  delays quitting.
- Put stdin into cbreak mode (`termios`/`tty`, POSIX only) for the session's
  duration, restored in a `finally:` covering normal exit, `Ctrl-C`, and any
  exception. Read keys off a background thread/reader into a queue the
  render loop drains; never a blocking read in the redraw loop itself.
- Panels: header (scheduler status, ready count, lifetime totals), an
  active-runs table (one row per `runs[]` entry from the snapshot: bead,
  recipe, status, stage, `iteration/max_iterations`,
  `consiliums/max_consiliums`, `tests_summary`, `elapsed_minutes`, a summary
  of `current_calls` and `tokens`), and a detail pane for the selected row
  (full `current_calls`, `judge` raw-vs-effective, worktree/branch).
- Controls: up/down or `j`/`k` to move selection, `enter`/`l` to toggle the
  detail pane, `q` to quit. The keymap's dispatch table must contain no
  handler that calls a mutating method on `Store`, `BeadsClient`, or the
  checkpointer -- verify this by construction and assert it in a test that
  inspects the dispatch table directly.
- Handle gracefully, without raising: a resize, the selected row's run
  disappearing between two snapshots (finished or errored out), zero active
  runs, and a `~/.alloy` with no prior runs.

### Acceptance Criteria

- Rendering logic (row-building, panel-building) is unit-tested directly
  against synthetic snapshot dicts shaped like Component 2's frozen JSON
  (call the function, assert on the resulting `rich` renderable's plain-text
  output or on intermediate row data before it reaches `rich`) -- not only
  through a manual run of the interactive command.
- A test asserts the keymap's dispatch table contains only read-only
  handlers (by inspection of the table, not by running the program and
  hoping nothing mutates).
- A test drives the controller with injected input events, an injected
  clock, and injected snapshots (no real terminal, no real timing-sensitive
  sleeps) and asserts: selection moves and clamps at the ends of a runs
  list, the detail pane toggles, `q` stops the loop, and a snapshot with
  zero runs or a disappeared selected row does not raise.
- Terminal mode is restored (a test can assert the cbreak-mode
  enter/restore calls both happened) even when the loop exits via a raised
  exception, not only on a clean quit.
- `python -m pytest -q` passes for the whole repo.

### Priority

2

### Type

feature

### Labels

alloy, monitor
