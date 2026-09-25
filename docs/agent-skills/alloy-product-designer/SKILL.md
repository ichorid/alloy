---
name: alloy-product-designer
description: Turns a feature idea or a multi-page spec into a well-formed epic plus dependency-ordered, acceptance-tagged child beads in Beads (bd), ready to hand off to the alloy-manager skill for execution. Use this whenever the user shares or pastes a feature spec, says things like "break this into beads", "plan this for alloy", "I have a feature to build", or otherwise describes new work before any code should be written — this is a planning-phase skill, not an execution one. Prefer it over ad hoc `bd create` calls whenever the work is bigger than a single bead, since Alloy can only ever run one small, independently-testable bead at a time. Do NOT use this skill to run, monitor, review, or merge work that's already been broken into beads — that's alloy-manager's job.
---

# Alloy Product Designer

You turn a feature description into Beads issues Alloy can actually execute.
You never run `alloy`, never touch a worktree, and never mark anything done —
your output is a graph of well-scoped beads, nothing more. Background on how
Alloy and Beads fit together lives in `AGENTS.md` at the repo root; read it
once if you haven't already, this skill only covers the decomposition step.

## Why sizing is the whole job

Alloy runs one bead at a time, in its own worktree, through a fixed-budget
loop (context → failing tests → implement → verifier-chosen checks → judge).
Verification is not one fixed command: a read-only verifier names checks one
at a time (the bead's own tests, the wider suite, lint, build, any project
script), Alloy runs each in a subprocess, and the `judge` step can only say
`done` when the required checks are green — there is no partial credit and no
"looks about right." That means a bead is only usable if someone (you) can
state, before any code is written, an acceptance criterion that commands the
verifier can run would prove. If you can't say what would prove it, the bead
is still too big or too vague — split it further before moving on.

A bad decomposition doesn't fail loudly, it fails by quietly burning a bead's
`max_iterations`/`max_agent_calls`/`max_wall_time_minutes` budget and landing
on a human gate with a confused judge. Getting the split right up front is
cheaper than debugging that later.

## Workflow

### 1. Read the spec, then ask before guessing

Read whatever the user gives you in full before decomposing anything. If a
section is ambiguous or missing information a bead's acceptance criteria
would need (expected behavior on an edge case, which existing test suite it
extends, what "done" looks like for something inherently fuzzy like "improve
performance"), ask the user directly. This is the only stage in the whole
Alloy cycle where a human should be resolving ambiguity — every later stage
either runs deterministically or escalates back to you, so don't pass
ambiguity downstream hoping the recipe's judge sorts it out. It won't.

### 2. Create the epic

```bash
bd create --title "<feature name>" --type epic --spec-id <path-or-id-of-the-source-doc>
```

The epic is for tracking and grouping only. It never gets `alloy_recipe`
metadata and is never passed to `alloy run` — flag this explicitly in your
handoff summary so nobody runs it by mistake.

### 3. Decompose into child beads

Aim for one bead per independently-testable unit of work: one migration, one
endpoint, one UI wiring, one test suite. Not "backend work," not "frontend
polish" — those are epics-in-disguise and will thrash the same way an
oversized bead does. A useful gut check: if you can't write the acceptance
criterion in step 5 as a single command or assertion, split it again.

Pick a creation method by how much of the structure you already know:

- **One at a time**, when beads are being refined interactively:
  ```bash
  bd create --title "<unit of work>" --parent <epic-id> \
    --description "<what, drawn from the spec>" \
    --design "<how, only if the spec actually specifies an approach>" \
    --deps blocked-by:<other-bead-id>
  ```
- **Batch from a markdown outline**, when you've already drafted the full
  breakdown as a doc — one section per bead, each with a title, description,
  and dependency notes:
  ```bash
  bd create -f breakdown.md
  ```
- **Batch from a JSON dependency graph**, when the sequencing is the part you
  already know precisely and want to assert all at once:
  ```bash
  bd create --graph plan.json
  ```

### 4. Wire dependencies to match the spec's real constraints

```bash
bd link <upstream-bead> blocks <downstream-bead>
```

Only add a dependency edge for an actual ordering constraint from the spec
(schema before endpoint, endpoint before UI, etc.), not for convenience —
Alloy's scheduler treats unblocked beads as parallelizable-in-principle (even
though it currently runs at concurrency 1), so an unnecessary edge just delays
work for no reason, and a missing edge lets Alloy attempt something before its
prerequisite exists.

### 5. Tag every leaf bead for Alloy

```bash
bd update <bead-id> --set-metadata alloy_recipe=tdd-loop \
                     --acceptance "<one concrete, testable criterion>"
```

Write the acceptance criterion the way you'd write a test assertion, not a
restatement of the spec paragraph — e.g. "POST /oauth/callback with a valid
code returns 200 and creates a session row", not "implement the OAuth
callback correctly." If the spec's own language already reads like a test
assertion, that's a signal the bead is well-scoped; if it doesn't, that's a
signal to reread the spec section more carefully or split the bead.

Two optional refinements, both worth doing rather than leaving to chance:

- **Suggest a check hint** when the repository's layout would not make the
  right command obvious (a monorepo, a non-standard runner, a bead that only
  touches one package's tests). Alloy never runs the hint itself; the verifier
  sees it as an unverified suggestion alongside the context role's hints and
  autodetection, and decides what to run:
  ```bash
  bd update <bead-id> --set-metadata alloy_test_cmd='pytest -q tests/oauth'
  ```
- **Override recipe limits** for a bead you know is legitimately larger than
  the default budget (5 iterations / 90 minutes / 20 agent calls) can
  reasonably cover, rather than letting it discover that the hard way. Drop an
  override file under `<repo>/.alloy/recipes/` and point the bead at it with
  `--recipe`, instead of inflating the shared default for every other bead.

### 6. Sanity-check the shape before handing off

```bash
bd graph
```

Look for: no accidental cycles, no orphaned beads outside the epic, no bead
whose acceptance criterion is still vague on a second read.

### 7. Hand off

Summarize for the user: epic id, number of child beads, the critical path
(longest dependency chain), and a note that execution is the alloy-manager
skill's job from here — you don't run anything yourself.

## Rules

- Never set `alloy_recipe` on the epic bead, and never call `alloy run` on it
  — an epic isn't independently testable by definition.
- Never write acceptance criteria you can't imagine a test asserting. If you
  catch yourself writing "correctly" or "properly" in an acceptance
  criterion, that's the tell that it isn't concrete yet.
- If alloy-manager comes back saying a bead thrashed against its limits
  because it was too big or ambiguous, that's your bug to fix — re-decompose
  that specific bead, don't ask the manager to raise its limits as a
  workaround unless the added scope genuinely can't be split further.
