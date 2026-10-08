# Alloy

Local-first orchestration for durable, adaptive coding-agent workflows.

Alloy runs one task at a time, in its own git worktree, through a checkpointed
LangGraph workflow that can call Claude Code, Codex, Cursor and Pi — and survives
the process, the terminal or the machine going away mid-task.

```
              Beads              durable work graph, source of truth
                │
         Alloy scheduler         picks READY work, claims it, no LLM
                │
            LangGraph            one checkpointed run per task
                │
    ┌───────────┼───────────┐
    ▼           ▼           ▼
  Claude      Codex     Cursor/Pi
    └───────────┼───────────┘
                ▼
          git worktree → tests → branch
```

## Two timescales, two owners

**Beads owns the project.** Tasks, priorities, dependencies, readiness, which
recipe to run, and the execution status Alloy writes back:

```
t-a3f  status: implementing
       alloy_recipe:   tdd-loop
       alloy_run_id:   019f2c…
       alloy_worktree: <repo>/.alloy/worktrees/t-a3f
       alloy_stage:    verify
```

**LangGraph owns the task.** Every step inside one run — context, tests,
implementation, verification, judgement, consilium, the human gate — lives in a
SQLite checkpoint, not on the bead.

## Supervising Alloy without polling

Alloy pushes; a supervising agent should not poll `alloy status`. Every time a
run needs a human, fails, stalls, resumes, finishes or is cancelled, Alloy
appends one JSON line to `<repo>/.alloy/events.jsonl`, and `alloy events` reads it:

```bash
alloy events --follow --attention      # block; print one line per needs-human/failed/stalled event
alloy events --since 1h                # catch up on what happened while you were away
alloy events --follow --since 30m --json
```

| event         | meaning |
|---------------|---------|
| `needs-human` | the run parked at the human gate; the reason says what to decide (`alloy resume <bead> -m "..."`) |
| `failed`      | the run ended failed; the worktree is kept |
| `stalled`     | a running run showed no sign of life for `--stall-minutes` (default 30, `alloy start --stall-minutes 0` disables) or its process died |
| `resumed`     | a parked run is running again |
| `done`, `cancelled` | informational |

Each line looks like `2026-09-25T10:04:11 needs-human alloy-x1 run=019f2c… <reason>`;
`--json` gives `{"ts","event","bead","run","reason"}`. A stall is announced
once, and again only if the run moves and then stalls anew. Activity means a
run update or an agent call starting or finishing, so one harness call
that legitimately runs longer than the threshold is reported too; raise
`--stall-minutes` if yours do.

### Instruction for a supervising agent

> Supervise Alloy for this repo. Do not poll. Start `alloy events --follow
> --attention` with the Monitor tool (persistent) and go idle; each stdout
> line is one event that needs you. On start-up, and whenever you are restarted,
> first run `alloy events --since 2h --attention` and `alloy status` once to
> catch up on anything you missed. For each event: `needs-human` -- read the
> reason, `bd show <bead>` and `alloy logs <bead>`, decide, then unblock with
> `alloy resume <bead> -m "<guidance>"`; if the decision is genuinely the
> owner's (product, security, spend), tell the owner instead of guessing.
> `failed` -- inspect `alloy logs <bead>` and the worktree, then retry with
> guidance, file a follow-up bead, or escalate. `stalled` -- check
> `alloy status <bead>`; if the harness is hung, `alloy cancel <bead>` then
> `alloy resume <bead>`; if the process died, restart `alloy start`. Ignore
> `done`/`resumed`. If the Monitor stream ends, restart it. Never act on the
> same event twice.

## Install

```bash
npm install -g @beads/bd            # the durable work graph
uv venv && uv pip install -e .      # Alloy itself
alloy init                          # <repo>/.alloy + Beads status registration
```

`alloy init` reports which harness CLIs it can see. Alloy drives them through
their own CLIs, so it uses your existing subscriptions — no API keys.

## The first milestone

```bash
bd q "add a slugify() helper to mypkg"
bd update t-a3f --set-metadata alloy_recipe=tdd-loop \
                --acceptance "slugify('Hello World') == 'hello-world'"

alloy run t-a3f
```

Alloy cuts a worktree on `alloy/t-a3f`, asks Cursor for context, asks Claude for
failing tests, asks Codex to implement, **runs the suite itself**, asks a judge
what to do next, and loops — up to the limits in the recipe.

```bash
alloy status --json         # machine-readable, for humans and manager agents
alloy monitor               # live view: runs, current agent, tokens, judge verdicts
alloy monitor --once --json # the same as one snapshot, for scripts
alloy limits [--json]       # probe installed harness usage limits; refresh limits.json
alloy logs t-a3f            # every agent call, with the path to its transcript
alloy resume t-a3f -m "use NFKD"
alloy cancel t-a3f          # a scheduler-owned run is stopped inside the scheduler, which keeps serving
alloy start                 # the polling scheduler, concurrency 1
alloy reconcile [--apply]   # find (and fix) beads whose bd status, alloy_* metadata and runs disagree
alloy assign-recipe tdd-loop-sonnet [--from tdd-loop] [--apply]   # retarget beads that pin a recipe
```

### Operating notes

- **Human-operated beads.** A bead labelled `manual` or `merge-gate`, or with
  `alloy_manual=true`, is never dispatched, run or landed, whatever
  `alloy_recipe` it carries. Epics with children are never run as tasks; they
  land once every descendant is closed.
- **Dispatch holds are re-announced.** When a review-ready bead or a run with
  no live process keeps every other top-level bead waiting for more than
  `--stall-minutes`, the scheduler logs a warning and emits a `stalled` event
  naming the holder and the command that clears it. `alloy reconcile <bead>`
  explains what is out of step.
- **Landing is bounded.** A closed bead is never landed or reopened. A bead
  with nothing to land (a tracking epic, work already on the target) is closed
  without a land run. An in-place land verifies the diff since the bead's
  first run. After three failed landings in a row the bead parks at
  waiting-human instead of filing another repair bug.
- **Recipe defaults only reach unassigned beads.** `alloy start --recipe` and
  `alloy:default:recipe` never override a bead's own `alloy_recipe`. The
  scheduler logs how many ready beads pin another recipe; `alloy
  assign-recipe` retargets them and skips epics and human-operated beads.
- **Fresh worktrees can run a setup hook.** An executable
  `<repo>/.alloy/worktree-setup` runs inside every newly created worktree
  (codegen, dependency fetch), with `ALLOY_PRIMARY_CHECKOUT` and
  `ALLOY_WORKTREE` set. A failure is logged and the run goes ahead. Processes
  still running inside an isolated worktree are stopped when a remediation
  child finishes, when its run is cancelled and when the worktree is removed.

## The `tdd-loop` recipe

```
context → write failing tests → implement → verify → judge → guard
                                    ↑                          │
                                    │                 done / retry /
                                    │               consilium / human / abort
                                    └── synthesize ← critics (parallel)
```

Three things are worth knowing about it:

**Verification is deterministic, and the checks are chosen, not fixed.** A
read-only `verifier` agent names one check at a time — the tests written for
the bead, the wider suite, lint, typecheck, build or any project script — and
Alloy runs it in a subprocess and reads the exit code. No LLM is asked to run a
shell command and report back, and no single test command is assumed: the
context role's `check_hints`, an optional `alloy_test_cmd` hint on the bead and
project-layout autodetection are handed to the verifier as unverified hints.
The `verification:` block of a recipe caps how many checks may run and for how
long; it never says what they are.

**The judge proposes; Alloy disposes.** `guard` is a plain Python function and it
is the only place the loop can continue from. It refuses `done` while tests are
red, downgrades a `consilium` request when the budget is spent, and escalates to
a human when a limit is hit. An agent cannot talk its way past a limit.

Cycles that do not pass through `guard` -- the reported-bug repair cycle
(implement → triage → implement), the verifier ↔ check loop, acceptance ↔
verifier, fast-track's implement ↔ check -- are gated too: every node that
starts agent work first checks the run's limits (`implement`, which starts a
new iteration, includes `max_iterations`; nodes inside an iteration check the
rest) and on a breach routes to `guard`, which parks the run (or budget-lands
it) exactly as for any other limit. Every implement call is an iteration,
bug-repair ones included. Wall time is also checked right before each agent
call, fallbacks included: once it is spent no new call starts (only `harvest`
and `memory_reviewer`, which run after the loop, are exempt).

```yaml
limits:
  max_iterations: 5
  max_consiliums: 1
  max_wall_time_minutes: 90
  max_agent_calls: 20
```

**Critics are independent.** Each consilium critic gets the same evidence packet
and none of its peers' opinions; a synthesizer reconciles them into one
instruction set. Critics run read-only and never edit code. A critic whose CLI
isn't installed is skipped, not fatal.

## Configuration

Graph logic is Python. YAML only says who plays which role:

```yaml
roles:
  context:   {runner: cursor-plan}
  tests:     {runner: claude-write, model: sonnet}
  implement:
    runner: astra                     # alias for codex
    fallback: {runner: claude-write, model: fable}   # if codex is missing or fails
  judge:     {runner: claude, model: sonnet}
```

Drop a file in `~/.alloy/recipes/` or `<repo>/.alloy/recipes/` to override the
built-in. A harness Alloy has never heard of needs no code:

```yaml
runners:
  myagent:
    binary: myagent
    args: ["--print", "--json", "{prompt}"]
    output: json_envelope
    text_field: result
```

### Per-run sandbox: a private, capped /tmp

Agents and checks used to leave multi-GB scratch in the system /tmp (a RAM
tmpfs) until it filled up. A recipe can give every run one bubblewrap
namespace whose `/tmp`, `/var/tmp` and `/dev/shm` are private tmpfs mounts
with a size cap; every agent call and check of the run shares it, and it is
gone when the run's processes exit:

```yaml
sandbox:
  mode: auto        # auto | bwrap | off
  tmp_size: auto    # auto = 50% of the alloy.slice memory limit; or 40% (of it) / 8G
  share: []         # host paths bound through, e.g. a /tmp dir a container mounts
```

`tdd-loop-sol-no-context` ships with `mode: auto`; other built-ins are `off`.
`auto` uses bwrap when a cached startup probe (`bwrap --bind / / --tmpfs /tmp
true`) works, otherwise it logs a warning and runs unsandboxed with a per-run
`TMPDIR` that is deleted at run end; `bwrap` refuses to start the run instead.
`ALLOY_SANDBOX=off` (or `auto`/`bwrap`) in the scheduler's environment overrides
every recipe -- the one-line rollback. With no slice limit detectable (not
started via `scripts/alloy-startup`), `auto` caps /tmp at 25% of RAM and warns.

How it works (see `src/alloy/sandbox.py`): the run stays in the scheduler (or
`alloy run`) process, so pids, adoption, cancel, `stop --now` and stall
detection are unchanged. At run start Alloy launches a small bwrap *holder*
(`--bind / /`, fresh `--dev /dev`, sized tmpfs mounts, no pid namespace), and
each harness and check is spawned as `nsenter -U -m --root` into
it; nsenter execs, so the recorded pid, process group and exit code are the
command's own. The host filesystem (repo, `$HOME`, harness auth, bd's dolt,
`/run/user/<uid>`, the docker socket) stays visible and writable; `TMPDIR=/tmp`
inside. The run's own worktree/log dir are bound through if they live under
/tmp. Caveats: dockerd resolves `-v` paths in the host namespace, so a
container mounting a path under the private /tmp sees the host's (empty) one
-- list such paths in `share`; host devices like `/dev/kvm` need `share` too;
setuid tools (sudo) do not work inside. The scheduler logs the probe result
and the resolved cap at start; `alloy status --json` shows them under
`sandbox.host` and each active run's sandbox under `sandbox.runs`.

## Project memory

Alloy keeps what it learns about a repository in Beads memories (`bd remember`,
`bd memories`, `bd forget`), not in git. At run start it reads the memories
once, renders a capped `## Project memory` block into every role's prompt, and
after the run writes back what it learned. Memory informs the agents; the
guard still decides. A remembered lesson never raises a limit, skips a check
or overrides a human gate.

Keys that start with `alloy:` are alloy-owned and carry a provenance trailer
(`[alloy run=<id> bead=<id> at=<date>]`); every other key is human-owned and
Alloy never rewrites it. What Alloy writes:

| key | what it holds |
|---|---|
| `alloy:check-hints` | check commands that proved useful, offered to the verifier as hints |
| `alloy:lesson:<key>` | lessons harvested from finished runs (why, and how to apply) |
| `alloy:human:<bead-id>` | operator guidance saved by `alloy resume --remember` |
| `alloy:calibration` | per-tier run statistics, shown only to the complexity estimator |
| `alloy:regression:<area>` | regression areas, injected role-specifically |
| `alloy:default:recipe` | the recipe the scheduler uses when a bead names none |
| `alloy:meta:*` | bookkeeping: last review date, embed set, stale-embed flag |
| `alloy:review:*` | reviewer contradictions and pending proposals |

`alloy:meta:*`, `alloy:review:*`, `alloy:regression:*`, `alloy:calibration` and
`alloy:default:recipe` never appear in the generic prompt block.

One human-owned key has a fixed meaning: `check-hints` holds check commands an
operator verified by hand (for example the full CI build sequence), one per
line, either bare or as `<kind>: <command>`. Runs never overwrite it, unlike
`alloy:check-hints`, and the verifier sees it as "operator-pinned checks
(verified by a human)", ahead of every unverified hint:

```bash
bd remember --key check-hints "build: flutter build web --wasm && dart run tool/apply_versioned_web_assets.dart"
```

```bash
alloy memory list                 # inventory: key, owner, provenance, age, flags
alloy memory review               # read-only review plan (stale, contradicted, proposed)
alloy memory review --apply       # execute the plan; only alloy-owned keys change
alloy memory embed                # splice the embed set into AGENTS.md / CLAUDE.md
alloy resume <bead-id> -m "..." --remember   # keep the guidance under alloy:human:<bead-id>
```

`alloy memory embed` writes only between the HTML-comment markers
`alloy:memory:begin` and `alloy:memory:end` in each file listed under
`memory.instruction_files` (default `AGENTS.md` and `CLAUDE.md`), appending a
block when the markers are missing. It never runs git. Review is due every
`review_every_days` (7) days or `review_every_runs` (20) runs; the scheduler
runs `alloy memory review --apply` and then `alloy memory embed`, skipping the
embed while an instruction file has uncommitted changes.

Every prompt is assembled from five layers in a fixed order: static (role
rules), project (memory), run (repository context and check hints), task
(brief and acceptance) and volatile (check results, diff, history). The
`prefix_hash` is the sha256 of the first three, recorded on every agent call;
the `prefix` column of `alloy logs` shows whether the cacheable prefix held
still across a run.

## Layout

Alloy stores each project's runs, checkpoints, logs, worktrees, events, and
scheduler files in that project's `.alloy` directory. `alloy monitor` reads
the current project's `.alloy` by default. `--repo` selects another project;
`--root` or `ALLOY_ROOT` overrides its state directory. `ALLOY_HOME` selects
the shared configuration directory, which defaults to `~/.alloy`.

```
<repo>/.alloy/
  alloy.db          runs + agent-call ledger
  events.jsonl      attention feed: needs-human, failed, stalled, ...
  workflows.db      LangGraph checkpoints
  logs/<run_id>/    raw transcripts and test output
  worktrees/<bead>/ one isolated checkout per task
  scheduler.pid     project scheduler lock
~/.alloy/
  recipes/          shared user recipes
  limits.json       shared harness limit samples
```

Graph state carries summaries and artifact paths. Full transcripts stay on disk,
so a long run does not turn into a context-window problem — and the ledger
(runner, model, prompt hash, duration, exit status, usage) is there to compare
recipes and harnesses on later.

```
src/alloy/
  beads.py       readiness in, execution status out
  worktree.py    isolation, one checkout per task
  verify.py      deterministic test execution
  runners/       one adapter per harness, behind one interface
  recipes/       tdd_loop.py (graph) + tdd-loop.yaml (config)
  runtime.py     what a graph node is allowed to touch
  engine.py      start, resume, settle
  scheduler.py   poll, claim, run — no LLM
  procs.py       process-tree control: a stopped run takes its harness with it
  usage.py       one shape for every harness's token report
  events.py      the attention feed (events.jsonl) behind `alloy events`
  store.py       runs, agent calls, in-flight calls — the ledger
  monitor/       snapshot.py (one read-only view), render.py, app.py (Textual)
  cli.py         init/run/start/stop/status/events/monitor/resume/cancel/logs/recipes
```

`docs/journal.md` records every surprise met while Alloy implemented its own
execution monitor, with what was done about each.

## Tests

Use `uv run pytest -n 0 -q <test-file>` for a focused check. Use
`uv run pytest -n 8 -q` for the bounded full regression suite.

AGENTS.md keeps one generated Beads integration block. Rerunning
`bd setup codex` may add a second Codex-specific block; remove that duplicate
while retaining the installed Codex hooks and skill files.

```bash
uv run pytest -n 0 -q tests/test_workflow.py  # focused check
uv run pytest -n 8 -q                        # bounded full suite
```

The suite runs in parallel by default; `-n 0` (or `-p no:xdist`) forces a
single worker with plain, ordered output when you need to debug a test.

The suite mocks the harness CLIs — stand-in binaries that emit each vendor's real
JSON envelope — so nothing spends tokens. Everything else runs for real: git
worktrees, SQLite checkpoints, the `bd` CLI, subprocess execution, and a
`SIGKILL` mid-run to prove a crashed task resumes from its checkpoint.
