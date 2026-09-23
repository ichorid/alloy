# Project memory: make Alloy adapt to the repo it works on

Source: operator request, 2026-09-22 (alloy-product-designer skill). Three
threads in one epic, because they share a substrate: `bd remember` memories
as a per-project, Dolt-synced, human-editable key/value store that Alloy
reads at the start of every run and writes to at the end.

## Problem

Every Alloy run starts cold. The context agent rediscovers the repo, the
estimator guesses complexity with no history, the test command is re-derived
per run, and the judge's `next_instructions` and every human resume message
die inside the run's LangGraph checkpoint. Alloy writes plenty *to* beads
(stage, complexity, notes, bug beads) but reads nothing project-wide back.

Separately, the prompts in `tdd_loop.py` interleave static instructions with
task-specific text (the bead brief is the second thing in every prompt), so
the provider-side prompt cache gets almost no prefix reuse across beads, and
adding a memory block naively would make it worse, not better.

## Thread 1 -- read memory into runs

- `BeadsClient` gains `memories()`, `remember(key, content)`, `forget(key)`
  over `bd memories --json`, `bd remember --key`, `bd forget`. An older `bd`
  without these commands yields an empty memory set, never an error.
- A `ProjectMemory` model normalises the raw dict: namespace (`alloy:` keys
  are Alloy-owned, everything else is human-owned), provenance trailer
  `[alloy run=<id> bead=<id> at=<YYYY-MM-DD>]` parsed off Alloy-written
  content, caps on item count and total bytes (defaults 12 items / 4000
  chars, from config), and a **deterministic** rendering: sorted by key,
  fixed format, no timestamps, so the rendered block is byte-identical
  between runs while the memory set is unchanged.
- Memory is read once per run, at run start, and stored in graph state so a
  resumed run and every iteration inside a run see the same snapshot.
- The context, estimate, tests, implement and judge prompts and the consilium
  evidence packet all receive the block. Bead design notes outrank memory;
  the prompts say so.
- The context role is asked to flag memories the repository contradicts
  (`memory_contradictions` in `CONTEXT_SCHEMA`). Contradictions never mutate
  the disputed memory; they are recorded as `alloy:review:contradiction:<key>`
  for the reviewer (Thread 3) and as a note on the bead.
- Memory informs, guard decides. Nothing read from memory can change a limit
  or let the loop continue. This invariant is tested.

## Thread 2 -- write memory from runs

- **Check hints.** (Re-scoped after `docs/plans/dynamic-verification.md`
  removed the single test command.) After a run finishes DONE, Alloy writes
  `alloy:check-hints`: the distinct runnable check commands the verifier used,
  as `<kind>: <command>` lines. The next run's verifier sees them under
  "Hints from the repository" ahead of the context role's hints and
  autodetection; the verifier still decides what to run. Unrunnable commands
  (exit 127, timeout) are never written.
- **Lesson harvest at finish.** A new read-only `harvest` role (default:
  claude, sonnet) sees the run's evidence packet, attempt history, human
  notes and the existing `alloy:lesson:*` keys and answers a structured
  `{lesson, key, confidence, scope}`. Write only when `scope == "repo"` and
  `confidence >= memory.harvest_min_confidence` (default 0.7), only for runs
  that finished DONE or FAILED after >= 2 iterations, and prefer updating an
  existing key over creating one. Failure of the role writes nothing.
- **Human guidance.** `alloy resume -m "..." --remember [KEY]` stores the
  message as `alloy:human:<bead-id>` (or KEY); with or without the flag the
  message reaches the harvest role as evidence.
- **Complexity calibration.** One key, `alloy:calibration`, holds a compact
  aggregate per level: runs, mean iterations, mean agent calls, overruns
  (limit hit). Finish updates it; the estimate prompt renders it as one line.
- **Regression awareness.** When a remediation bead is not merged
  (`engine.py` unmerged-remediation path) Alloy writes
  `alloy:regression:<top-level-path>`; the consilium evidence packet lists
  regression memories whose path prefix matches a relevant file.
- **Default recipe.** If a READY bead has no `alloy_recipe` and the scheduler
  has no `--recipe`, `alloy:default:recipe` supplies one. Limits are never
  read from memory.

## Thread 3 -- prompt stability for cache reuse

Provider caches match on exact prefix. Every prompt is therefore assembled by
one function, `alloy/prompts.py::assemble`, from five ordered layers:

1. **static**  -- role instructions, rules, `BUG_PROTOCOL`, structured-output
   schema instructions. Identical for every run of that role, ever.
2. **project** -- rendered project memory. Identical across runs while the
   memory set is unchanged.
3. **run**     -- repository context packet, check hints. Identical
   across iterations inside one run.
4. **task**    -- bead brief and acceptance criteria.
5. **volatile**-- test results, attempt history, diff, this iteration's
   instructions, budget line. Always last.

Rules the layer builder enforces: no run ids, timestamps, worktree paths,
iteration counters or clock values above the volatile layer; `_render_*`
helpers are pure functions of their input; the runner puts schema
instructions in the static layer instead of appending them to the tail.
Golden files under `tests/golden/prompts/` pin the exact layout of every
role's prompt so an accidental reordering fails CI. The agent-call ledger
records `prefix_hash` (sha256 over layers 1-3) next to the existing
`prompt_hash`, and `alloy logs` shows it, so cache stability across
iterations and across runs is measurable, not assumed.

## Thread 4 -- review facility and instruction-file embedding

- `alloy memory list [--json]` shows key, owner, provenance, age, flags.
- `alloy memory review` builds a plan: a deterministic hygiene pass (expired
  Alloy-owned memories past `memory.ttl_days`, contradiction flags whose
  subject key is gone, byte-identical duplicates) plus a read-only
  `memory_reviewer` role that sees every memory, the contradiction flags,
  the current embedded block and a repo tree summary and returns, per key,
  `keep | update | forget | embed` with a reason and optional new content.
  Without `--apply` it prints the plan and writes nothing.
- `--apply`: verdicts on Alloy-owned keys are applied. Verdicts that would
  forget or rewrite a **human-owned** key are never auto-applied; they are
  recorded as `alloy:review:proposal:<key>` and one bead labelled
  `alloy-memory-review` is created for a human. `alloy:meta:last-review`
  and `alloy:meta:embed` (sorted list of keys marked `embed`) are updated.
- `alloy memory embed` renders the `alloy:meta:embed` set into a managed
  block between `<!-- alloy:memory:begin -->` and `<!-- alloy:memory:end -->`
  in every file in `memory.instruction_files` (default: `AGENTS.md`, and
  `CLAUDE.md` when it exists). Deterministic ordering, idempotent, appends
  the block when the markers are absent, replaces only the block when they
  are present, never commits. The block header carries `reviewed: <date>` and
  `review due: <date>`.
- The scheduler runs review (apply, Alloy-owned only) followed by embed when
  `alloy:meta:last-review` is older than `memory.review_every_days` (default
  7) or more than `memory.review_every_runs` (default 20) runs finished since,
  at most once per day, never while a run is active. Embed is skipped with a
  logged reason and a note when the instruction file has uncommitted changes.
- The embedded block itself is reviewed: at run start Alloy compares the
  block in the worktree against the current embed set and the `reviewed`
  date; a mismatch or a block older than twice the review interval sets
  `alloy:meta:embed-stale` which makes the next review due immediately. The
  reviewer receives the block text, so hand edits inside the markers are
  reviewed too before being regenerated.

## Configuration

A `memory:` block in the recipe YAML, parsed like `VerifySpec`:

```yaml
memory:
  enabled: true
  max_items: 12
  max_chars: 4000
  ttl_days: 90
  harvest_min_confidence: 0.7
  review_every_days: 7
  review_every_runs: 20
  instruction_files: [AGENTS.md, CLAUDE.md]
roles:
  harvest:         {runner: claude, model: sonnet}
  memory_reviewer: {runner: claude, model: sonnet}
```

## Non-goals

- Memory never changes limits, never bypasses `guard`, never commits.
- No cross-repo memory: the store is the project's own Dolt DB.
- No embedding of Alloy-owned volatile keys (`alloy:meta:*`,
  `alloy:review:*`, `alloy:calibration`) into instruction files.

## Cost

One `bd memories --json` per run (~0.2 s). One `bd remember` at finish, one
after the first parsed verify. Review runs at most daily.
