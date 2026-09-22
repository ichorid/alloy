# Complexity-tiered runners and a stop-the-line bug pipeline

Source: operator request, 2026-09-22, revised the same day after a design
critique (alloy-product-designer skill). Two features. The complexity work is
built first because the bug triage reuses its classifier plumbing: one Jev
enum question with a deterministic default on failure.

## Feature 1 -- complexity estimator with tiered runners

Before the implementer runs, Jev estimates the task's complexity from its
description, acceptance criteria and the context packet: `simple`, `medium`
or `complex`. Each class maps to an ordered runner list -- first entry is the
priority, the rest are fallbacks in order. Recipe YAML:

```yaml
complexity:
  routing: shadow              # shadow: estimate and record only; live: dispatch by tier
  escalate_after_retries: 2    # live only: consecutive judge retries before bumping one tier
  tiers:
    simple:  [{runner: cursor, model: composer-2.5}, {runner: codex, model: gpt-5.6-luna}, {runner: claude-write, model: haiku}]
    medium:  [{runner: claude-write, model: sonnet}, {runner: cursor, model: composer-2.5}]
    complex: [{runner: astra}, {runner: claude-write, model: fable}, {runner: cursor, model: kimi-k3-high}, {runner: claude-write, model: opus}]
roles:
  implement: {runner: astra, fallback: {runner: claude-write, model: fable}, tiered: true}
  tests:     {runner: cursor, model: composer-2.5, fallback: {runner: claude-write, model: sonnet}}
```

Decisions:

- **Shadow mode first.** Nothing shows Jev's labels mean anything for "how
  hard is this task" yet. The estimator always runs and records; dispatch
  only changes when `routing: live`. The ledger then answers, per tier,
  iterations-to-done and cost-per-done-bead before routing is trusted.
- A role opts in with `tiered: true` and keeps its normal runner for shadow
  mode. No sentinel runner name, so recipe validation and the fallback display
  keep working.
- A tier compiles to the existing `RoleSpec` fallback chain: unavailable,
  non-zero exit and timeout already advance to the next entry.
- **Fallback is availability, not capability.** A weak model that exits 0
  with wrong code never yields. In live mode, after `escalate_after_retries`
  consecutive judge retries the run bumps one tier and the ledger records it.
- Only `implement` is tiered. `tests` is pinned to cursor composer-2.5 (with
  a Sonnet fallback) because the tests are the spec the judge enforces.
  `context` stays on read-only cursor-plan, `judge` stays fixed.
- The complex tier's third entry is a non-Claude runner: the Claude session
  limit is per account (journal 38), so Fable then Opus buys nothing when the
  limit hits. Codex has been out of credits on every recorded run, so the
  tier chains are diversified across vendors.
- Operator override `alloy_complexity=<tier>` skips the estimator. The
  estimate itself is recorded under `alloy_complexity_estimated` and on the
  run row, so a rerun after failure re-estimates instead of freezing a guess.
- Cursor reports errors inside an exit-0 envelope. That must count as a
  failed call or the simple tier's head silently ships garbage with no
  fallback. Same for a codex `error` event with no agent message.
- `alloy recipes --probe` sends one trivial prompt through every tier entry
  so unverified model ids (`gpt-5.6-luna`, `kimi-k3-high`, the `opus` and
  `haiku` aliases) are checked before a real run depends on them.
- `JevRunner` picks the first enum-valued property, so every classifier
  schema puts its enum first.

## Feature 2 -- stop the line on a blocking bug, fix it, continue

Andon is pulled for defects that stop the line, not for observations. The
goal is full autonomy: a blocking bug is fixed in place, systematically, as a
normal bead would be, and the human is involved only when the fix would
subvert the task's goal or require an architecture-sized change.

- The context, tests and implement prompts get one short rule: if you find
  a defect in existing code outside this task, do not fix it and do not route
  around it silently. **If it blocks your task, stop and report it. If it does
  not, finish your work and append the report.** Reports are `<bug>` blocks
  (title, where, evidence, blocks_task yes/no).
- Reports from every role are captured into state. Only the `implement`
  stage routes to triage; reports from context and tests ride along to the
  first triage after implement.
- `triage` (Jev primary, Sonnet fallback) sees the task, acceptance,
  current test results, the report and the bugs already filed in this run,
  and answers one of five labels:
  - `not-a-bug` -- the report is the task itself, or the tests it was asked
    to make pass. Nothing filed; the implementer is told so and continues.
    This closes the escape hatch a stuck implementer would otherwise have.
  - `duplicate` -- already filed in this run. Noted, continue.
  - `non-blocking` -- filed as a bug bead, priority 3, label `alloy-bug`,
    `discovered-from` the running bead. Continue with an instruction naming
    the bead and forbidding the fix here.
  - `blocking` -- filed at priority 1 with label `alloy-bug`, claimed at
    once so a polling scheduler cannot steal it, and **remediated
    immediately** as a child run (below). The parent resumes when the fix is
    merged into its worktree.
  - `needs-human` -- blocks the task, but fixing it needs an architectural
    change or a decision outside the task (schema change, public API change,
    dependency swap, behaviour the acceptance criteria contradict). Filed at
    priority 1 with labels `alloy-bug` and `human`, parent made blocked-by
    it, run parks at the human gate. This is the operator's carve-out, not a
    default.
- Triage failure parks at the human gate with the report in the reason and
  files nothing.
- If the implementer stopped (said `blocks_task: yes`) and triage did not
  say blocking, implement runs again with the verdict; if it finished, the
  run goes to verify.
- Bug beads never get priority 0: an agent-filed bead must not outrank every
  human-prioritised bead forever. Labels and `bd human list` are the review
  path.
- The filed bug bead gets a generated acceptance criterion the child's judge
  can enforce: the defect no longer reproduces per the reporter's evidence,
  the existing suite stays green, and the change touches only what the
  defect requires.

### Remediation as a child run

- The parent's uncommitted work is committed on its branch as a WIP commit
  so the merge has a clean tree to land on.
- The bug bead's worktree and branch `alloy/<bug id>` are created from the
  **parent's base commit** (the commit the parent branched from), not from
  the WIP commit. The defect is by definition in pre-existing code, so it
  reproduces at base; the child then works against a green suite and never
  sees the parent's red tests or half-done edits. A defect that only appears
  with the parent's changes is in scope and triage must label it
  `not-a-bug`.
- The child is a normal run: same recipe, own run id, own checkpoint thread,
  own budget, `parent_run_id` on the ledger, the estimator and tiers apply.
- On child `done`, one deterministic guard runs: the child's diff must not
  modify or delete any test file the parent's WIP commit added. A fix that
  removes the task's own tests can never be right, so no model decides that.
- Everything else about scope is Jev's call, not a cap. A `scope` role (Jev
  primary, Sonnet fallback) sees the **project context packet** and the
  child's diff and answers `merge`, `too-broad` (an architecture-sized change
  for a bug fix) or `subverts-task` (the fix changes behaviour the parent's
  acceptance criteria depend on). Only `merge` proceeds; the other two park
  the parent at the human gate with Jev's label and reason.
- The project context packet is what "the total context of the project"
  means in practice, assembled by Alloy before each scope or triage call:
  - the project brief: `.alloy/project.md` when the operator wrote one, else
    the first part of README.md;
  - the bead graph: open and in-progress beads (id, type, priority, title,
    status), the parent bead's epic with its description, and the bugs Alloy
    has filed so far;
  - progress: `bd stats` counts, the parent run's attempt history and
    iteration, remediations already done in this run and their outcomes.
  Clipped to a fixed size so it fits Jev's state input and stays cheap.
- Triage gets the same packet. A second or third blocking bug in one run is
  not cut off by a counter; Jev sees the remediations so far and the run's
  progress and decides `blocking` again or `needs-human`.
- The deterministic backstop is the parent bead's own budget: the child's
  agent calls and wall time count against the parent's `max_agent_calls` and
  `max_wall_time`. Total work on a bead, including the bugs it uncovered, is
  bounded by the limits the operator already set, and only a human resume
  extends them. Depth stays one: a blocking bug found inside a child is
  filed and the child parks.
- Conflict, a failed guard, a non-merge scope verdict, a child that failed
  or parked at its own human gate all park the parent at the human gate
  with the reason. A normal bead's failure path is the human gate too.
- The parent resumes at `implement` with an instruction naming the fixed
  bug and forbidding undoing the fix. Its own verify and judge then run as
  usual, so a merged fix that breaks the parent is caught by the parent's
  loop.

## Open questions for the operator

- When to flip `routing: live`: proposal, after a dozen shadow-mode beads
  show estimate versus iterations-to-done.
- Whether `kimi-k3-high` via cursor is the right non-Claude entry for the
  complex tier, or codex Luna.
- Whether counting child time and calls against the parent's budget is the
  right backstop, or whether remediation should get its own budget line in
  the recipe limits.
- What belongs in `.alloy/project.md` so Jev's scope calls are grounded;
  without it the README head is all Jev sees of the project.
