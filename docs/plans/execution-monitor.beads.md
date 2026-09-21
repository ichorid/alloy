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
  and `Store.token_totals_by_role(run_id)` built on it. `cost_usd` in those
  totals is the sum of reported costs, or `None` when no record reported one
  -- never `0.0` for "unreported" (see Component 1 item 4).
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
  fields treated as `0` in the sum. `cost_usd` is `None` when no row
  reported a cost and the sum of the reported costs otherwise.
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
  `Store.token_totals(run_id)` and `Store.token_totals_by_role(run_id)`
  (as `tokens` and `tokens_by_role`), the raw-vs-effective judge pair from
  the most recent judge-role `agent_calls` row, and requested-vs-effective
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
  specifically), not omitted. `tokens_by_role` is `{}` for a run
  with no calls and has one entry per role otherwise.
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

## Alloy monitor: Textual live view (runs table, refresh, quit)

Add the default (no `--once`) `alloy monitor` dashboard as a Textual app:
stats line, runs table, worker-driven refresh, `j`/`k`/`q`, plus the plain
`alloy monitor --once` rendering. No detail pane yet (next bead). Depends on
the snapshot-assembly bead (its `build_snapshot` and CLI scaffold must
already be merged to `main`).

### Description

Full rationale, widget structure, worker/refresh rules and the pilot-based
testing approach: see "Component 3 -- interactive `alloy monitor` view
(Textual)" in `docs/plans/execution-monitor.md` (committed at the root of
this repository -- read it before writing any code). `textual>=1.0` is
already a declared dependency; do not add any other UI library and do not
touch `termios`/`tty`.

### Design

- Add `src/alloy/monitor/render.py` with pure functions `run_rows(snapshot)
  -> list[tuple[str, ...]]` (one tuple per `runs[]` entry: bead, recipe,
  status, stage, `i/max`, `c/max`, tests, elapsed, now, tokens, judge -- the
  exact column rules are in the plan doc) and `header_line(snapshot) -> str`.
- Add `src/alloy/monitor/app.py` with `MonitorApp(textual.app.App)`:
  `snapshot_source: Callable[[], dict]` and `interval: float` constructor
  arguments; `compose()` yields `Header`, a `Static#stats`, a
  `DataTable#runs` with `cursor_type="row"`, and `Footer`; `on_mount()`
  does one refresh and `set_interval(interval, refresh_snapshot)`;
  `refresh_snapshot` is a `@work(thread=True, exclusive=True)` worker that
  calls `snapshot_source()` off the event loop and applies the result via
  `call_from_thread`; a raising worker leaves the last good snapshot on
  screen and puts "refresh failed: <type>" in the stats line.
- `apply_snapshot(snapshot)` rebuilds the table with rows keyed by `run_id`
  and keeps the cursor on the same `run_id` when it still exists, otherwise
  clamps to the last row (or no cursor when there are no runs).
- `BINDINGS`: `j`/`down` cursor down, `k`/`up` cursor up, `q` quit. Actions
  only touch widget state.
- Extend the `monitor` Typer command: no flags → `MonitorApp(...).run()`;
  `--once` without `--json` → print `header_line` and a Rich table built from
  `run_rows` to stdout and exit 0; `--interval` (float, default 1.0).

### Acceptance Criteria

- `run_rows` and `header_line` are unit-tested against synthetic snapshot
  dicts shaped exactly like the plan doc's frozen JSON, including: zero
  runs, a run with two `current_calls`, a run with `judge: null`, and a run
  whose raw and effective judge decisions differ.
- Pilot tests (`async with MonitorApp(...).run_test() as pilot`, no real
  terminal) assert: the table shows one row per run after the first refresh;
  `j`/`k` move the cursor and clamp at both ends; `q` exits with return
  code 0; a snapshot with zero runs renders without raising; a snapshot in
  which the previously selected run is gone leaves the cursor on a valid
  row; a `snapshot_source` that raises leaves the previous rows visible and
  does not crash the app.
- `alloy monitor --once` (no `--json`) exits 0 and prints the header line and
  one table row per active run; with no runs it prints the header line and
  an empty table without raising.
- The refresh worker runs off the event loop: a test with a
  `snapshot_source` that blocks for 0.5 s must still process `q` within that
  window (use the pilot; do not sleep-and-hope).
- `python -m pytest -q` passes for the whole repo.

### Priority

2

### Type

feature

### Labels

alloy, monitor

## Alloy monitor: detail pane and judge panel

Add the selected-run detail pane to the Textual dashboard: `enter`/`l`
toggles it, it shows in-flight calls, the judge's raw vs effective verdict,
per-role token totals and the run's paths. Also adds the read-only-bindings
test. Depends on the Textual live view bead (its `MonitorApp` and `render.py`
must already be merged to `main`).

### Description

Full rationale and the exact content rules for the detail pane: see
"Component 3 -- interactive `alloy monitor` view (Textual)" in
`docs/plans/execution-monitor.md` (committed at the root of this repository
-- read it, in particular the "Panels" paragraph and the raw-vs-effective
judge rules in Component 1 item 3, before writing any code).

### Design

- Add `detail_lines(run: dict) -> list[str]` to
  `src/alloy/monitor/render.py`: every `current_calls` entry as
  `role: requested_runner[:model] -> effective_runner[:model] (42s)`; the
  judge as `judge said <raw decision> (confidence 0.61) / Alloy did
  <effective decision> -- <reason>` when they differ, one line when they
  match, and `judge: no parseable verdict` when `raw` is null while
  `effective` is not; `tokens_by_role` as one `role: total (in/out)` line
  per role; worktree, branch and log dir.
- Add a `RunDetail(Static)` widget (id `detail`, `display = False` initially)
  to `MonitorApp.compose()`, rendered from `detail_lines` for the run under
  the cursor; `apply_snapshot` refreshes it when visible.
- Add bindings `enter`/`l` → `toggle_detail`. The pane hides itself when the
  selected run disappears and there is no row to select.
- Add a test that inspects `MonitorApp.BINDINGS`: each action must be in the
  allowlist `{cursor_down, cursor_up, toggle_detail, quit}`, and the source
  of `alloy/monitor/app.py` and `alloy/monitor/render.py` must contain none
  of `finish_run`, `update_run`, `create_run`, `set_status`, `set_metadata`,
  `claim`, `cancel(`, `note(`.

### Acceptance Criteria

- `detail_lines` is unit-tested against synthetic run dicts covering: two
  in-flight calls with requested != effective runner; matching raw/effective
  judge; differing raw/effective judge; `raw` null with `effective` set
  (must yield "no parseable verdict"); `judge: null`; empty
  `tokens_by_role`.
- Pilot tests assert: `enter` shows the detail pane for the selected run and
  `enter` again hides it; `l` behaves like `enter`; moving the cursor with
  the pane open updates its content; a snapshot in which the selected run
  disappeared hides the pane instead of raising.
- The bindings/read-only test described in Design exists and passes.
- `python -m pytest -q` passes for the whole repo.

### Priority

2

### Type

feature

### Labels

alloy, monitor
