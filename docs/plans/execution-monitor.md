# Execution Monitor — architecture & implementation plan

Status: revised after review by codex/Astra (read-only review, 2026-09-21),
then revised again the same day after a second review (Claude): the
interactive view now uses Textual instead of hand-rolled terminal handling,
`cost_usd` aggregation is specified, per-role token totals reach the
snapshot, and the view bead is split in two. See git history for the diffs.

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
(each summed *token* field is `0` if every contributing record was `None` for
that field, so a run with genuinely no usage data returns zeros, matching the
already-agreed acceptance criterion, without conflating "reported zero" and
"never reported"). `cost_usd` is the exception: it is the sum of the reported
costs when at least one record reported one, and `None` when none did --
the frozen JSON below shows `"cost_usd": null`, and a `0.0` there would claim
a free run rather than an unpriced one.

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
- `tokens_by_role`: `Store.token_totals_by_role(run_id)` -- `{role: tokens}`
  with the same four-field shape per role (`{}` when the run has no calls
  yet). The detail pane (Component 3) shows it; the table shows `tokens`.
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
      "tokens_by_role": {
        "context": {"input_tokens": 4000, "output_tokens": 300, "total_tokens": 4300,
                    "cost_usd": null},
        "implement": {"input_tokens": 4123, "output_tokens": 212, "total_tokens": 4335,
                      "cost_usd": null}
      },
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
"total_tokens": 0, "cost_usd": null}` and `"tokens_by_role": {}`.

### Component 3 — interactive `alloy monitor` view (Textual)

Built on **Textual** (`textual>=1.0`, the TUI framework from Rich's author;
`textual` is a declared dependency in `pyproject.toml`). Textual owns the
terminal lifecycle (raw mode enter/restore, alternate screen, resize), key
bindings, timers and background workers, and ships a headless test harness:
`App.run_test()` yields a `Pilot` that can `press()` keys, `pause()`, and
inspect widgets, with no terminal and no real timing. Every hand-rolled piece
of the previous revision (termios cbreak mode, a key-reading thread, a queue,
restore-in-`finally`) is therefore deleted, and the acceptance tests become
ordinary `pytest-asyncio` tests.

Layout of the code (`src/alloy/monitor/`):

- `snapshot.py` -- `build_snapshot(engine) -> dict` (Component 2, unchanged).
- `render.py` -- pure functions from a snapshot dict to row tuples and
  detail text: `run_rows(snapshot) -> list[tuple[str, ...]]`,
  `header_line(snapshot) -> str`, `detail_lines(run: dict) -> list[str]`.
  Unit-tested directly against synthetic snapshots shaped like the frozen
  JSON; no Textual involved.
- `app.py` -- `MonitorApp(App)`:
  - constructor: `snapshot_source: Callable[[], dict]` (defaults to
    `lambda: build_snapshot(engine)`; tests inject a lambda returning a
    fixture) and `interval: float` (seconds; default 1.0 from `--interval`).
  - `compose()`: `Header`, a `Static` header line (id `stats`), a
    `DataTable` (id `runs`, `cursor_type="row"`), a `RunDetail(Static)`
    widget (id `detail`, `display = False` until toggled), `Footer` (renders
    the bindings automatically).
  - `on_mount()`: one immediate `refresh_snapshot()` plus
    `self.set_interval(self.interval, self.refresh_snapshot)`.
  - `refresh_snapshot()` is a `@work(thread=True, exclusive=True)` worker:
    it calls `snapshot_source()` **off** the event loop (`BeadsClient`
    shells out with a 60 s timeout and SQLite can busy-wait 30 s; neither
    may delay `q`), then hands the result to `apply_snapshot` via
    `self.call_from_thread(...)`. While a refresh is in flight the stats line
    ends with "refreshing…" and the previous snapshot stays on screen. If the
    worker raises, the exception is logged (`self.log`), the stats line shows
    "refresh failed: <ExceptionType>", and the last good snapshot remains.
  - `apply_snapshot(snapshot)`: rebuilds the `DataTable` rows from
    `render.run_rows`, keyed by `run_id` (`add_row(*cells, key=run_id)`),
    keeps the cursor on the same `run_id` when it still exists, otherwise
    clamps to the last row (or none when `runs` is empty); updates the stats
    line and, if visible, the detail pane for the selected run.
  - `BINDINGS`: `j`/`down` → `cursor_down`, `k`/`up` → `cursor_up`,
    `enter`/`l` → `toggle_detail`, `q` → `quit`. Every `action_*` method
    touches only widget state; none imports or calls a mutating method on
    `Store`, `BeadsClient` or the checkpointer. Read-only-ness is asserted by
    a test that (a) checks each action named in `BINDINGS` is in an explicit
    allowlist and (b) scans `alloy/monitor/app.py`'s and `render.py`'s
    source for the tokens `finish_run`, `update_run`, `create_run`,
    `set_status`, `set_metadata`, `claim`, `cancel(`, `note(` -- none may
    appear.
- CLI (`src/alloy/cli.py`): `alloy monitor` (no flags) →
  `MonitorApp(...).run()`. `alloy monitor --once --json` is unchanged from
  bead 2. `alloy monitor --once` (no `--json`) prints `render.run_rows` as a
  Rich table plus the header line to stdout and exits 0 -- useful over ssh
  without a TTY, and a free by-product of `render.py`.

Panels. Stats line: scheduler running/pid, ready count with "(capped at
1000)", lifetime done/failed/cancelled, age of the displayed snapshot. Runs
table columns: bead, recipe, status, stage, iter (`i/max`), cons (`c/max`),
tests, elapsed, now (`role:runner[:model] 42s`; several in-flight calls
joined with ` + `; `-` when none), tokens (`total_tokens`, or `in/out` when
both known), judge (`raw→effective` when they differ, else the decision, or
`-`). Detail pane for the selected run: every `current_calls` entry with
requested vs effective runner/model and elapsed; `judge` raw vs effective
with confidence, or "no parseable verdict" when raw is null; `tokens_by_role`
as one line per role; worktree, branch and log dir paths.

Testing with the pilot, all headless:

```python
async def test_q_quits():
    app = MonitorApp(snapshot_source=lambda: TWO_RUNS, interval=1000)
    async with app.run_test() as pilot:
        await pilot.pause()          # let the first refresh land
        await pilot.press("j", "enter", "q")
    assert app.return_code == 0
```

`interval=1000` effectively disables timed refreshes in tests; tests inject
snapshots by swapping `app.snapshot_source` (or calling
`app.apply_snapshot(...)` from inside `run_test`) to cover zero runs, a
selected run disappearing between two snapshots, and a snapshot_source that
raises. Resize is exercised with `run_test(size=(80, 24))` and
`(200, 50)`.

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

One epic (tracking only, no recipe) with four leaf beads, in dependency
order:

1. **Data layer** -- Component 1 in full.
2. **Snapshot assembly & `alloy monitor --once --json`** -- Component 2 in
   full. Depends on (1).
3. **Textual live view: runs table, refresh, quit** -- Component 3 minus the
   detail pane: `render.py`, `MonitorApp` with the stats line, the runs
   table, the worker-driven refresh, `j`/`k`/`q`, cursor preservation, and
   `alloy monitor --once` (plain). Depends on (2).
4. **Detail pane and judge panel** -- the rest of Component 3: `RunDetail`,
   `enter`/`l`, raw-vs-effective judge, `tokens_by_role`, the read-only
   bindings test. Depends on (3).

A fifth, follow-up bead records the harness process id on `inflight_calls`
so `alloy cancel` and crash recovery can kill a harness whose `alloy run`
parent died without cleaning up (journal entry 9). It depends on (1) and is
not part of the monitor's acceptance.

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
