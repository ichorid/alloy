# Auto-land: merge finished work into the target branch

Today a successful run stops at `review-ready` and a human merges
`alloy/<bead-id>` by hand. This plan makes Alloy land finished work itself.

## Decisions (2026-09-23)

- **Unit of landing.** A standalone bead (no epic ancestor) lands as soon as
  its run succeeds. Children of an epic run serially in one shared worktree
  owned by their **top-most epic ancestor** (`<worktrees>/<epic-id>`, branch
  `alloy/<epic-id>`); the epic lands when every descendant is closed.
  Sub-epics are grouping only and never land separately.
- **Epic children close immediately.** When a child's run succeeds, Alloy
  commits its changes on the epic branch (`<child-id>: <title>`) and closes the
  child, unblocking the next sibling. A failed or waiting-human child blocks
  every other child of the same epic (the shared tree is not clean).
- **Where the merge happens.** The final merge is `git merge --no-ff --no-edit
  alloy/<id>` in the **primary checkout**, which must be on the target branch
  (`landing.target`, default `main`). If the primary checkout is on another
  branch or git refuses because local changes would be overwritten, nothing is
  touched and the bead parks at `waiting-human` (an environment problem, not a
  code problem).
- **Verification before the target moves.** Before touching the primary
  checkout, Alloy does a **trial merge**: it merges the target into the
  bead/epic branch inside its own worktree and runs the normal verifier check
  loop + judge against the merged tree (the bead's acceptance + the wider
  suite). Only a green judge proceeds to the primary merge.
- **Landing is a recipe.** `land` is a registered recipe (`land.yaml` + graph
  builder) reusing tdd-loop's verifier/judge steps. It is invoked by the engine
  (`Engine.land`, `alloy land <id>`) and the scheduler — never via
  `alloy_recipe` metadata.
- **Failures become remediation beads.** A trial-merge conflict or red
  post-merge checks files a `bug` bead (`discovered-from` the landed bead, which
  it `blocks`, recipe `tdd-loop`) that runs **in the landed bead's worktree**
  (metadata `alloy_worktree_owner=<id>`). For a conflict its task is "merge
  `<target>` into this branch and resolve conflicts in <files>"; for red checks
  the trial-merge commit stays on the branch and its task is "make <check>
  green". When the repair bead closes, the scheduler retries landing.
- **Opt-in per recipe.** `landing: {mode: off|auto, target: <branch>}` in the
  recipe YAML; the dataclass default is `off`, the shipped `tdd-loop` and
  `tdd-loop-jev` recipes set `auto`.
- **After landing.** Bead (or epic) closed, worktree removed, branch deleted,
  note with the merge sha. Bead metadata: `alloy_land_state`
  (`pending|landed|repairing|parked`), `alloy_land_sha`, `alloy_land_repair`.
