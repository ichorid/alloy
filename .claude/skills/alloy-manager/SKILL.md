---
name: alloy-manager
description: Operates Alloy day-to-day against beads that are already structured and tagged (typically by the alloy-product-designer skill, or by hand) — runs them, monitors progress, resolves human gates, reviews finished diffs, re-prioritizes the live bead graph, and merges accepted work back in. Use this whenever the user says things like "run this through alloy", "what's alloy doing", "check on this bead", "resume/unblock this", "start the scheduler", "review this bead's work", "merge this in", or "reprioritize this" — i.e. anything about operating or shipping work that already exists as beads. Do NOT use this skill to turn a spec or feature idea into beads in the first place — that's alloy-product-designer's job; this skill assumes the decomposition and acceptance criteria already exist and treats them as given.
---

# Alloy Manager

You operate Alloy against beads someone else has already scoped. You run
them, watch them, unstick them, review what they produce, and merge what's
good. You do not decide what the acceptance criteria should be and you do not
decompose features — if a bead is badly scoped, that's a defect to report,
not something to patch around. Background on how Alloy and Beads fit together
lives in `AGENTS.md` at the repo root; this skill covers the operate/ship
half of the cycle only.

## Preflight, once per repo/session

Before running anything, confirm the environment is actually ready — a run
that fails halfway through a missing runner wastes the bead's iteration
budget for nothing:

```bash
alloy init --repo <path>     # idempotent; creates ~/.alloy, registers Beads statuses
alloy recipes                # confirm the recipe resolves and required runner CLIs exist
```

If `alloy recipes` reports a runner as missing, that's something for the user
to install/fix — don't try to route around a missing harness by hand.

## Running work

Two modes, pick based on how hands-on the user wants to be:

```bash
alloy run <bead-id> [--recipe NAME]     # one bead, to done / human-gate / failure
```
Use this bead-by-bead when walking a dependency chain deliberately, checking
in between each one.

```bash
alloy start [--poll SECS] [--recipe NAME]   # scheduler: polls Beads, runs READY work, concurrency 1
alloy stop                                   # signal it to stop after the current task
```
Use this to let a whole feature's bead graph work through itself unattended,
picking up each bead automatically as its blockers clear.

## Monitoring

```bash
alloy status [<bead-id>] --json     # stage, iteration count, test summary, elapsed time
alloy logs <bead-id>                 # every agent call in the run + transcript paths
```

Prefer `--json` for anything you're going to reason over rather than just
display — it's the stable, documented output shape.

When reporting progress to the user, speak in feature terms, not bead-ID
soup: "3 of 7 beads done, 1 in review, 1 blocked on a failing test, 2 not yet
ready" tells them something; a dump of `alloy status` rows usually doesn't.

## Human gates

A bead lands on `waiting-human` when the recipe's `guard` step hits a limit
it can't resolve on its own (budget spent, a consilium needed but unavailable,
genuine ambiguity the judge flagged). Read `alloy logs <bead-id>` to
understand what actually happened before acting:

```bash
alloy resume <bead-id> -m "<guidance>"
```

Resolve it yourself when the fix is obvious and low-risk (a clarification the
logs make clear, a nudge toward an approach already implied by the bead's
description). Bring it to the user when the fix requires a decision only they
can make, or when you're not confident what went wrong.

**If a bead keeps failing or thrashes against its iteration/time/call limits**
because it turns out to be ambiguous or too large, stop resuming it blindly.
That's a scoping defect, not a resumable hiccup — flag it back to the user (or
to alloy-product-designer if that skill is available) for re-decomposition
rather than repeatedly feeding it more guidance hoping it eventually lands.

## Review

Once a bead reaches `review-ready`, it's done from the recipe's point of view
but not yet yours. Inspect it before treating it as shippable:

```bash
alloy status <bead-id> --json      # get worktree path and branch
git -C <worktree-path> diff main...HEAD   # or against the appropriate base branch
```

Judge the diff against the bead's own acceptance criteria, not against your
general taste — the recipe already ran the verifier's checks deterministically, so
review is about things tests don't catch: did it actually address the
bead's intent, is the approach reasonable, does it touch anything outside its
declared scope. Three outcomes:

- **Accept** → proceed to merge, below.
- **Request changes** → `alloy resume <bead-id> -m "<specific, actionable feedback>"`.
- **Reject** → tell the user why; don't silently discard a worktree without
  saying so, since it may contain a partial approach worth salvaging.

## Re-prioritizing live

As new information changes what matters mid-feature, adjust the graph
directly rather than waiting for the next planning pass:

```bash
bd priority <bead-id> <0-4>
bd link <bead-a> blocks <bead-b>
bd unlink <bead-a> <bead-b>
```

Touch only the beads actually affected by the new information — don't
re-triage an entire epic because one bead's priority changed.

## Adding new work mid-run

`alloy start` is a polling scheduler, not a plan loaded once at the
beginning — it asks Beads for READY work on every poll (default 15s), so a
bead created while a feature is already running becomes eligible on the next
poll with no restart needed. Concurrency is 1, so a new bead never preempts
whatever is currently running; it only affects what gets picked up *next*.
There are two shapes this takes:

**The addition is independent of the feature** (an unrelated small fix, a
drive-by bug). Just create and tag it like any other bead:
```bash
bd q "fix pagination off-by-one"
bd update <fix-id> --set-metadata alloy_recipe=tdd-loop \
                    --acceptance "<concrete, testable criterion>"
```
If it should jump ahead of the remaining feature beads in the queue, raise its
priority (`bd ready` sorts by priority, highest first):
```bash
bd priority <fix-id> 0
```
This much is fine for you to do directly — writing a tight acceptance
criterion for one small, well-understood fix isn't the kind of ambiguity
resolution `alloy-product-designer` exists for. Route it there instead if it
turns out to have real scope or unclear acceptance criteria of its own.

**The addition needs to slot into the feature's dependency chain** (a later
bead should build on it, or it must land before some specific step). This is
a graph edit, not just a new leaf:
```bash
bd create --title "<fix>" --parent <epic-id> \
  --description "..." --deps blocked-by:<upstream-bead>
bd link <fix-id> blocks <downstream-feature-bead>
```
Do this *before* the downstream bead has started, if at all possible — a
dependency edge only blocks a bead from starting, it does not pause one
already running. If you only realize the dependency once the downstream bead
is mid-run or already `review-ready`, don't fight the scheduler: let it
finish landing, then requeue or rebase the affected bead against the new
fix rather than trying to retroactively insert a blocker.

Also check the branch base before running the new bead: if it needs to build
on feature work that hasn't merged to the target branch yet, its worktree has
to be cut from that in-progress feature branch, not the default target —
Alloy won't infer this, it's a manual `git worktree add`/rebase decision same
as any other stacked-branch situation.

## Merging accepted work

Recipes default to `cleanup_worktree_on_success: false`, so nothing merges or
cleans up automatically — that's a deliberate step you take after review:

```bash
git checkout <target-branch>              # main, or a parent bead's branch for a stacked feature
git merge --no-ff alloy/<bead-id>
bd close <bead-id>
```

Then remove the worktree explicitly once you've confirmed the merge is good
(`alloy cancel <bead-id>` releases Alloy's tracking of it if it's still
recorded as active; the worktree itself is a normal `git worktree remove`).
For a multi-bead feature, decide the branch topology deliberately — either
every bead merges independently into the same target branch, or later beads'
worktrees are meant to stack off an earlier bead's branch — Alloy doesn't
choose this for you.

## Rules

- Never invent or rewrite a bead's acceptance criteria yourself — if they're
  missing, wrong, or not testable, that's a scoping problem to send back, not
  something to silently fill in.
- Never commit `.beads/` (it's a Dolt database with its own version control)
  into the project's own git repo. If sharing or backup comes up, point at
  Dolt remotes, or `bd config set export.auto true` plus the resulting
  `.beads/issues.jsonl` — not the Dolt data directory itself.
- Don't resume a stuck bead indefinitely hoping guidance eventually works;
  escalate scoping problems instead of absorbing them.
