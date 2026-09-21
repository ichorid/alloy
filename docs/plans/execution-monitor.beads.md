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
