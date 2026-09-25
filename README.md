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
alloy cancel t-a3f
alloy start                 # the polling scheduler, concurrency 1
```

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

```bash
pytest          # runs in parallel via pytest-xdist (-n auto)
pytest -n 0     # serial, for debugging (or: pytest -p no:xdist)
```

The suite runs in parallel by default; `-n 0` (or `-p no:xdist`) forces a
single worker with plain, ordered output when you need to debug a test.

The suite mocks the harness CLIs — stand-in binaries that emit each vendor's real
JSON envelope — so nothing spends tokens. Everything else runs for real: git
worktrees, SQLite checkpoints, the `bd` CLI, subprocess execution, and a
`SIGKILL` mid-run to prove a crashed task resumes from its checkpoint.
