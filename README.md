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
       alloy_worktree: ~/.alloy/worktrees/t-a3f
       alloy_stage:    verify
```

**LangGraph owns the task.** Every step inside one run — context, tests,
implementation, verification, judgement, consilium, the human gate — lives in a
SQLite checkpoint, not on the bead.

## Install

```bash
npm install -g @beads/bd            # the durable work graph
uv venv && uv pip install -e .      # Alloy itself
alloy init                          # ~/.alloy + Beads status registration
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

**Verification is deterministic.** Alloy runs the test command in a subprocess
and reads the exit code. No LLM is asked to run a shell command and report back.

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

## Layout

```
~/.alloy/
  alloy.db          runs + agent-call ledger
  workflows.db      LangGraph checkpoints
  logs/<run_id>/    raw transcripts and test output
  worktrees/<bead>/ one isolated checkout per task
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
  store.py       runs, agent calls, in-flight calls — the ledger
  cli.py         init/run/start/stop/status/resume/cancel/logs/recipes
```

`docs/journal.md` records every surprise met while Alloy implemented its own
execution monitor, with what was done about each.

## Tests

```bash
pytest
```

The suite mocks the harness CLIs — stand-in binaries that emit each vendor's real
JSON envelope — so nothing spends tokens. Everything else runs for real: git
worktrees, SQLite checkpoints, the `bd` CLI, subprocess execution, and a
`SIGKILL` mid-run to prove a crashed task resumes from its checkpoint.
