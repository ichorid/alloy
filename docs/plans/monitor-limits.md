# Monitor: harness usage limits, per-model usage in bead details, finished beads of the current session

Extends `alloy monitor` (see `docs/plans/execution-monitor.md` for the existing
snapshot shape and the read-only contract of `build_snapshot`). Three additions:

1. **Limits section.** A gauge block showing the usage limits of every harness
   that is installed on this machine, refreshed on its own slow interval. The
   gauges are harness-specific: Claude Code has a 5-hour window, a weekly
   window and per-model weekly windows (Opus, Fable, ...); Codex has a 5-hour
   ("primary") and a weekly ("secondary") window; Cursor has a billing-cycle
   total gauge and, when the dashboard reports it, a second API-usage gauge
   for the same cycle.
2. **Per-model usage in the detail pane.** For the selected run, every
   `(runner, model)` pair that made at least one agent call, with its call
   count and tokens, joined to the current limit windows of the harness it
   rides on.
3. **Finished beads of the current session.** The runs table shows not only
   active runs but every run that finished (done / failed / cancelled) since
   the current scheduler session started.

All new snapshot fields are always present (`null` when absent, never
omitted), exactly like the existing shape. `build_snapshot` stays synchronous,
read-only and network-free: limit probes run elsewhere and leave a cache file
that the snapshot reads.

## Vocabulary

- **Harness**: a CLI Alloy drives. Three are limit-aware here: `claude`
  (binary `claude`), `codex` (binary `codex`), `cursor` (binary
  `cursor-agent`). Runner names map onto harnesses:
  `claude`, `claude-write` → `claude`; `codex`, `codex-readonly`, `astra` →
  `codex`; `cursor`, `cursor-plan` → `cursor`; anything else → `null`.
  A harness is **installed** when `RunnerRegistry.available(<runner>)` is
  true for its canonical runner name.
- **Window**: one usage gauge. Frozen shape:
  ```json
  {"key": "five_hour", "label": "5h", "used_percent": 42.0,
   "resets_at": "2026-09-22T17:00:00+00:00", "model": null}
  ```
  `used_percent` is a float 0..100 (may exceed 100 if the harness reports
  so); `resets_at` is ISO-8601 UTC or `null`; `model` is the model family a
  per-model window applies to (`"opus"`, `"fable"`, ...) or `null` for the
  account-wide window.
- **HarnessLimits**: everything known about one harness:
  ```json
  {"harness": "claude", "installed": true, "available": true,
   "fetched_at": "2026-09-22T14:50:00+00:00", "as_of": "2026-09-22T14:50:00+00:00",
   "source": "oauth-usage-api", "error": null, "status": null,
   "windows": [ ...Window... ]}
  ```
  `available` is false and `error` a short string when the probe could not
  produce windows (no credentials, HTTP error, no local sample). `as_of` is
  the time the numbers were true (for Codex, the timestamp of the session
  event they came from; for network probes, equal to `fetched_at`). `status`
  is an optional harness-reported condition string (Codex's
  `rate_limit_reached_type`, e.g. `workspace_member_credits_depleted`).

## Component A — `alloy.limits` package

`src/alloy/limits/__init__.py` (shapes, mapping, cache) plus one module per
probe: `claude.py`, `codex.py`, `cursor.py`.

### A1. Core (`alloy/limits/__init__.py`)

- `HARNESSES = ("claude", "codex", "cursor")`; `RUNNER_HARNESS` mapping as in
  Vocabulary; `harness_for_runner(name) -> str | None`.
- `installed_harnesses(registry: RunnerRegistry) -> list[str]` — in
  `HARNESSES` order, those whose canonical runner is available.
- `window(key, label, used_percent, resets_at, model=None) -> dict` and
  `unavailable(harness, error, *, installed=True) -> dict` constructors that
  always return the full shapes above.
- Cache: `AlloyPaths.limits_cache` = `<root>/limits.json`.
  `write_cache(paths, samples: dict[str, dict])` writes atomically
  (temp file + `os.replace`); `read_cache(paths) -> dict[str, dict]` returns
  `{}` for a missing or unparsable file, never raises.
- The probe protocol: `probe(paths_or_env) -> dict` returning a HarnessLimits
  dict. Every probe is pure over injected inputs (a fetcher callable and a
  home directory) so tests never touch the network or the real home.

### A2. Claude Code probe (`alloy/limits/claude.py`)

- Credentials: `~/.claude/.credentials.json`, key `claudeAiOauth.accessToken`
  (also read `expiresAt`; a token past expiry is reported as
  `error: "token expired"`, not used).
- Request: `GET https://api.anthropic.com/api/oauth/usage` with headers
  `Authorization: Bearer <token>`, `anthropic-beta: oauth-2025-04-20`,
  `Accept: application/json`, 10 s timeout. The HTTP call goes through an
  injectable `fetch(url, headers) -> (status, body)` so tests use fixtures.
- Response parsing (tolerant): every top-level key whose value is a dict with
  a `utilization` field becomes a window. `five_hour` → label `5h`,
  `seven_day` → label `weekly`, `seven_day_<model>` → label `weekly <model>`
  with `model=<model>`. This is what gives Fable (and Opus) its own gauge:
  the probe does not hard-code a model list. `resets_at` is passed through
  as ISO-8601; `null` values are skipped (no window).
- Errors: missing file / key → `available: false, error: "no credentials"`;
  non-2xx → `error: "HTTP <status>"`; malformed JSON → `error: "bad response"`.
- `source: "oauth-usage-api"`.

### A3. Codex probe (`alloy/limits/codex.py`)

Codex writes its rate-limit state into every session rollout it runs,
including `codex exec` sessions Alloy starts, so no network call is needed:

- Scan `~/.codex/sessions/**/rollout-*.jsonl`, newest first by mtime, and
  read only the tail of each file (last 64 KiB is enough) for lines whose
  `payload.type == "token_count"` and whose `payload.rate_limits.limit_id ==
  "codex"` with a non-null `primary`. The first such event (newest) wins.
- Windows: `primary` → key `primary`, label `5h` when `window_minutes == 300`
  (otherwise `f"{window_minutes//60}h"`); `secondary` → key `secondary`,
  label `weekly` when `window_minutes == 10080` (otherwise
  `f"{window_minutes//1440}d"`). `used_percent` from `used_percent`;
  `resets_at` from the epoch-seconds `resets_at`, converted to ISO-8601 UTC.
- `as_of` = the event's `timestamp`. `status` = `rate_limit_reached_type`
  from the newest `token_count` event of *any* `limit_id` (this is where
  "credits depleted" shows up), else `null`.
- No matching event anywhere → `available: false, error: "no local sample"`.
- `source: "session-rollout"`.

### A4. Cursor probe (`alloy/limits/cursor.py`)

- Credentials: `~/.config/cursor/auth.json`, key `accessToken` (a JWT; the
  `sub` claim is `<provider>|<user_id>`).
- Primary request: `POST
  https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage`
  with `Authorization: Bearer <accessToken>`, `Connect-Protocol-Version: 1`,
  and an empty JSON body; 10 s timeout; injectable fetcher as in A2.
- Dashboard parsing: read `planUsage.totalPercentUsed` into one window, key
  `total`, label `cycle`; `resets_at` from `billingCycleEnd` (epoch ms →
  ISO-8601 UTC). When `planUsage.apiPercentUsed` is present as a number,
  append a second window, key `api`, label `API`, with the same `resets_at`.
  When `apiPercentUsed` is absent, the probe returns only the cycle window.
- Legacy fallback: when the dashboard response has no usable `planUsage`,
  `GET https://cursor.com/api/usage?user=<user_id>` with cookie
  `WorkosCursorSessionToken=<user_id>%3A%3A<accessToken>`; parse per-model
  `numRequests` / `maxRequestUsage` plus `startOfMonth` into **one** window
  (key `total`, label `cycle`; `resets_at` = `startOfMonth` plus one month).
  If no entry has a positive `maxRequestUsage`, `available: false,
  error: "no quota in response"`.
- `source: "dashboard-api"` on the primary path; `source: "usage-api"` on
  the legacy fallback.

### A5. `probe_all` and `alloy limits`

- `probe_all(paths, registry, *, fetch=None, home=None) -> dict[str, dict]`
  runs the probe of every installed harness (A1 detection), catches every
  exception per probe into an `unavailable(...)` entry, writes the cache and
  returns the mapping keyed by harness. Harnesses that are not installed are
  **not** in the mapping.
- CLI: `alloy limits [--json]` calls `probe_all` and prints either the
  mapping as JSON or a table with columns `harness`, `window`, `used`,
  `resets`, `as of`, `note` (note = `error` or `status`). Documented in
  AGENTS.md's command list and README's command list.

## Component B — data layer

### B1. `Store.models_used(run_id)`

`list[dict]` ordered by first use: one entry per distinct `(runner, model)`
among the run's `agent_calls` rows (and its in-flight rows, which count as
`calls: 0` if they have no finished call yet):
```json
{"runner": "claude-write", "model": "fable", "harness": "claude",
 "calls": 3, "tokens": {"input_tokens": .., "output_tokens": .., "total_tokens": .., "cost_usd": null}}
```
`tokens` uses the existing `_sum_usage` over `normalize()`. `harness` comes
from `harness_for_runner`.

### B2. `Store.finished_runs_since(since: str, repo: Path | None)`

Runs with `status` in `TERMINAL_RUN_STATUSES`, `ended_at >= since` (ISO
string comparison is fine — all timestamps are `utcnow().isoformat()`),
optionally filtered by repo, ordered by `ended_at` ascending.

### B3. Scheduler session file

`AlloyPaths.scheduler_session` = `<root>/scheduler.json`. `Scheduler.serve`
writes `{"pid": .., "started_at": <iso>, "ended_at": null}` right after the
pidfile, and sets `ended_at` in its `finally`. `read_session(paths) -> dict |
None` returns the parsed file or `None` (missing / unparsable). The pidfile
and `read_pid` are unchanged. A session whose `ended_at` is `null` but whose
pid is dead is still a valid "last session" for the snapshot's purposes.

## Component C — snapshot (`alloy.monitor.snapshot`)

New top-level keys, always present:

- `"session": {"started_at": <iso|null>, "ended_at": <iso|null>, "pid": <int|null>}`
  from B3 (all null when no session file).
- `"limits": {<harness>: HarnessLimits}` from `read_cache` — every
  *installed* harness appears; one that is installed but has no cache entry
  yet appears as `unavailable(harness, "not probed yet")`. Not-installed
  harnesses are absent.
- `"runs"` now = active runs (as today, unchanged order) followed by
  `finished_runs_since(session.started_at, repo)` rendered through the same
  `_run_entry`, excluding any run already present. When `session.started_at`
  is null, only active runs are listed.
- Each run entry gains `"models_used": [ ...B1 entries, each with "windows":
  [...] ]` where `windows` is the harness's current windows filtered to
  account-wide ones plus the per-model window whose `model` equals the
  entry's model family (`fable`, `opus`, ... — match on the model string
  containing the family name, case-insensitive). Unknown harness → `[]`.
- `"lifetime"` is unchanged; add `"session_totals": {"done": n, "failed": n,
  "cancelled": n}` counted over the finished runs included in the snapshot.

`--once --json` prints the whole thing. `build_snapshot` never probes.

## Component D — render (`alloy.monitor.render`)

- `limits_lines(snapshot) -> list[str]`: one line per harness, e.g.
  `claude   5h 42% (resets 17:00)  weekly 61%  weekly fable 12%  weekly opus 80%`;
  unavailable harness → `codex    unavailable: no local sample`; a `status`
  is appended as `[credits depleted]`. Empty list when `limits` is empty.
  Percent is rendered as an integer; `as_of` older than 30 minutes appends
  `(as of HH:MM)`.
- `header_line` gains `this session: done N failed M cancelled K` when
  `session_totals` is present.
- `detail_lines` adds, after the per-role token lines, one line per
  `models_used` entry:
  `model claude-write:fable  calls 3  tokens 12000  |  5h 42%  weekly 61%  weekly fable 12%`
  (the window part is omitted, not blank, when `windows` is empty).
- `run_rows` needs no change for finished runs (the `status` column already
  carries done/failed/cancelled), but `_now` must render `-` for a run with
  no current calls (already true).
- `alloy monitor --once` (plain text) prints `limits_lines` after the header
  and before the table.

## Component E — live view (`alloy.monitor.app`)

- New `Static(id="limits")` between the stats line and the runs table,
  hidden while `limits_lines` is empty.
- `MonitorApp.__init__` gains `limits_source: Callable[[], dict] | None`
  and `limits_interval: float = 60.0`. When `limits_source` is given, a
  `@work(thread=True, exclusive=True, group="limits")` worker calls it on
  mount and every `limits_interval` seconds; the result is merged into the
  snapshot's `limits` on the next `apply_snapshot` (the CLI wires
  `limits_source=lambda: probe_all(...)`, which also refreshes the cache the
  snapshot reads, so no extra plumbing is required beyond triggering the
  probe). Failures keep the last good limits and log, exactly like
  `refresh_snapshot`.
- `alloy monitor` gains `--limits-interval SECS` (default 60) and
  `--no-limits` (skip probing; the section then shows the cache only).
- Detail pane, cursor and toggle semantics are unchanged; finished runs are
  selectable like active ones.

## Component F — parent bead column (monitor and `alloy status`)

Child remediation runs (those with `parent_run_id` set on the ledger, see
alloy-0uc.8) must show their **parent bead id** explicitly so operators can
see parent/child relationships at a glance. This supersedes the `└ ` indent
prefix on the `bead` column from alloy-0uc.9.

### Snapshot

Each `runs[]` entry gains `parent_bead_id: str | null`, always present.
When `parent_run_id` is set, resolve it with
`store.get_run(parent_run_id)["bead_id"]`; otherwise `null`. Keep
`parent_run_id` as-is for machine consumers.

### Render (`alloy.monitor.render`)

- `COLUMNS` gains `parent` as the **leftmost** column, before `bead`.
- `_row`: first cell is `parent_bead_id` or `-`; the `bead` cell is the run's
  own `bead_id` with no tree prefix.
- The interactive monitor table and `alloy monitor --once` both use `COLUMNS`
  / `run_rows`, so they pick this up automatically.

### `alloy status`

- `--json`: each bead row gains `parent_bead_id` (same resolution as the
  snapshot). `parent_run_id` stays for callers that need the run id.
- Plain-text table: leftmost column `parent` (min width ~10), then the
  existing columns unchanged. Child remediation rows show the parent bead id;
  all other rows show `-`.

Implemented in bead **alloy-w9d.10** alongside the limits render work (same
`COLUMNS` / `run_rows` surface).

## Multi-bead execution

Every bead below is independently testable with `pytest`. Branch each bead's
worktree off `main`; the beads touch mostly disjoint files, and the snapshot
and render beads only depend on the *shapes* fixed in this document, so they
can be written against literal dicts before the upstream beads land. Where a
bead needs a symbol from an upstream bead (e.g. `harness_for_runner`), the
dependency edge in Beads enforces the order.

Test command for every bead: `/home/vader/MY_SRC/alloy/.venv/bin/python -m pytest -q`.
