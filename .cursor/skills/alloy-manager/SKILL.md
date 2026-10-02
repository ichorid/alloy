---
name: alloy-manager
description: Autonomously operates Alloy until every bead is done — runs the scheduler, watches the attention feed, resolves human gates, remediates failures and landing problems, fixes recurring issues at their source, commits and pushes. Use whenever the user says things like "run this through alloy", "what's alloy doing", "unblock this", "start the scheduler", "land/merge this", "finish the beads", or anything about operating or shipping work that already exists as beads. Do NOT use it to turn a spec into beads; that is alloy-product-designer's job.
---

# Autonomous Alloy Operator

## Goal and authority
Your goal is to get every open bead closed, with its work landed on `main` and pushed.
You may run and cancel beads, resume human gates, edit code and tests, remediate, commit, push, edit bead metadata, and restart the scheduler.
Act rather than ask. Stop and escalate only for the cases under "Hard limits".

## Start of session
1. `bd prime`, `git status`, `git pull --rebase`.
2. `alloy reconcile`. Apply fixes only after checking that the underlying cause is really gone.
3. Make sure a scheduler is running: `alloy status --json`. If none is, run `alloy start`.
4. Tail `alloy events --follow --attention`. Every ~15 min, also run `alloy status --json`.

## Bead intake: pre-seed complexity and recipe
Small tasks going through the full TDD-loop pipeline (tests role, triage, verifier, acceptance, judge) waste calls they don't need. Alloy no longer estimates complexity with an LLM call when a bead already carries `alloy_complexity` metadata — the `estimate` node short-circuits to that override. So: before (or as part of) activating any bead that doesn't yet carry `alloy_complexity`/`alloy_recipe` — including bugs Alloy's own triage auto-files, which you don't create by hand — set both:

```
bd update <bead> --set-metadata alloy_complexity=<simple|medium|complex> --set-metadata alloy_recipe=<recipe-name>
```

Rule:
- **simple**, or TDD is a poor fit by your own judgment (pure config/doc edits, one-liners, environment/investigation tasks with no meaningful red/green cycle) → `alloy_recipe=fast-track`. One role implements, proposes and runs its own real checks, and self-judges done/retry — no separate tests/verifier/acceptance/judge round.
- **medium** → `alloy_recipe=tdd-loop-medium-no-context`.
- **complex** → `alloy_recipe=tdd-loop-complex-no-context`.

Do this during the periodic grooming sweep too, to backfill any bead that slipped through without this metadata — not only at the moment a bead is first created.

## Main loop: react to each event
- **nothing dispatches or a `stalled` hold**: run `alloy reconcile <holder>`.
  - holder waiting on an open repair bug: let it run, or fix it yourself (below).
  - holder whose run has no live process: `alloy run <holder>` adopts it; `alloy cancel <holder>` drops it.
- **needs-human**: read the reason, then verify the tree yourself (run the tests, lint, build). Then do one of:
  - Resume with concrete guidance: `alloy resume <bead> -m "<exact fix or decision>"`.
  - Fix the problem yourself in the checkout, commit, and resume with "fixed in <sha>; verify".
  - If the run is poisoned (same gate after 2 resumes): `alloy cancel <bead>`; the scheduler re-runs it fresh.
- **failed**: read `alloy logs <bead>` and the bead notes.
  - Environment problem: fix it, `bd update <bead> -s open`, let it re-run.
  - Task problem: tighten the description or acceptance, or split the bead into smaller ones, then reopen.

A bead runs directly on the primary checkout and finishes `done` on its own once its own in-loop broader check passes — there is no separate land step or `review-ready`-awaiting-land state to act on.

## Fix things once, at the source
- **Check-command gaps**: when a failure recurs, put the verified command in `bd remember --key check-hints "<kind>: <full command>"`, or fix the target repo's script or test. Never fix it only for one run.
- **Recipe switch**: `alloy assign-recipe <new> --apply`. It skips epics and manual beads.
- **Tests that compare against `main`** break in in-place mode: fix or drop that comparison in the target repo.
- **Oversized remediation rejected as too-broad**: cherry-pick the files listed as needed, commit, then resume the parent.

## Doing work by hand
- Beads run in place on the primary checkout, always. Don't edit the checkout while a bead runs; stop first (`alloy stop`, then wait).
- Commit before letting Alloy start again: an in-place start refuses while tracked files are uncommitted.
- Finishing a bead yourself: run the quality gates (tests, lint, build), commit, `git push`, `bd close <bead>`, then `alloy reconcile <bead> --apply` to clear stale run state.
- Epics close once all their children are closed. Don't run epics.
- Checklists and gates: label them `manual`, complete them yourself, then close them.

## Commits and pushes
- Commit small and often, with messages naming the bead.
- After each manual fix: `git pull --rebase && git push`. If the push fails, resolve it and retry; never force-push `main`.
- Run the full suite before any push that touches shared code.

## Hard limits: escalate instead of acting
- Destructive operations: force-push, history rewrite, deleting branches with unmerged work, dropping data.
- Changing a bead's acceptance criteria in a way that weakens what was asked, or deleting or skipping tests to get green.
- Secrets, credentials, production deploys, spending or external services beyond what the project already uses.
- The same bead failing 3 fresh runs after your interventions. Write up the evidence on the bead, label it `human`, and move on.

## Session end
When no open beads remain besides those escalated to a human:
1. Run the full quality gates.
2. `git pull --rebase && git push`.
3. `alloy reconcile` shows nothing out of step.
4. Report what landed (SHAs), what you fixed by hand and why, what you escalated, and any `check-hints` or setup hooks you added.
