# Monitor task tree — pinned QUEUE row and nested epics

Source: chat design session, 2026-09-23. Builds on
`docs/plans/monitor-tui-redesign.md` (panel chrome, status colors, width tiers).

## Goal

`DataTable#runs` in `src/alloy/monitor/app.py` becomes a collapsible task tree:

1. A pinned **QUEUE** row is always the first row. When collapsed it shows the
   ready/blocked counts and the next bead to dispatch. When expanded
   (`enter`) it lists the queue.
2. Runs are **grouped under their epic**. An epic row shows a summary and can
   be expanded to show its children. Beads with no epic stay top-level, below
   the epics.

## Decisions (confirmed with the operator)

| # | Question | Decision |
|---|----------|----------|
| 1 | Show blocked beads in the expanded queue? | Yes, after ready beads, each with its blockers (`blocked by a, b`). |
| 2 | Queue cap | 50 ready beads in the expanded queue, then `… N more`. |
| 3 | Done children of an epic | Folded into one `✓ N done (ids…)` line, the last child. |
| 4 | Queued bead inside an epic too? | Yes, it appears under QUEUE and under its epic (twice is fine). |
| 5 | Narrow widths (<80) | Tree indentation may take space from the `bead` column. |

## Data (snapshot additions, `src/alloy/monitor/snapshot.py`)

All fields are always present (`[]`/`0`/`null`, never omitted), per the frozen-
shape rule in the module docstring. `ready_count` stays as it is.

```jsonc
"queue": {
  "ready": [            // dispatch order: same filter + order as Scheduler.next_task, max 50
    {"bead_id": "...", "title": "...", "recipe": "tdd-loop", "priority": 2,
     "complexity": "medium" | null, "epic_id": "..." | null}
  ],
  "ready_total": 7,     // uncapped count of dispatchable ready beads
  "blocked": [
    {"bead_id": "...", "title": "...", "blocked_by": ["..."], "epic_id": "..." | null}
  ]
},
"epics": [
  {"epic_id": "...", "title": "...", "total": 9, "done": 4,
   "done_ids": ["..."], "running": 2, "judge": 1}
]
```

Every `runs[]` entry also gets `"epic_id"` (nearest epic ancestor or `null`).
`epics[]` lists every epic that appears as an `epic_id` in `runs[]` or `queue`.
If `bd` fails, `queue` is `{"ready": [], "ready_total": 0, "blocked": []}` and
`epics` is `[]`; the view still shows the runs.

## Rendering (pure, `src/alloy/monitor/render.py`)

`task_tree_rows(snapshot, expanded: set[str], width) -> list[TreeRow]`, where
`TreeRow` has `key`, `kind` (`queue | queued | blocked | more | epic | run |
done_fold`), `depth` and `cells`. Row keys: `"queue"`, `"queue/<bead>"`,
`"epic/<id>"`, `"run/<run_id>"`, `"epic/<id>/done"`.

- QUEUE row, collapsed: `▸ QUEUE`, status cell `N ready · M blocked · next: <id>`.
- QUEUE expanded: `▾ QUEUE`, then numbered ready rows (up to 50), a `… N more`
  row when `ready_total > 50`, then `⊘` blocked rows showing `by a, b`.
- Epic row: `▸/▾ <epic_id>  <title>`, summary `R running · J judge · D/T done`.
- Epic children, in this order: its runs, then its queued beads, then the done fold, all
  drawn with `├─` / `└─`.
- Top-level runs (no epic) come after all epic rows.

## Interaction (`src/alloy/monitor/app.py`)

- `enter` on QUEUE or epic: toggle expand. `enter` on a run: toggle detail, as today.
- `expanded` persists across snapshot refreshes. The cursor stays on the
  same row key.
- Detail pane for QUEUE: dispatch-order note plus counts. For an epic: title,
  progress, done ids.
- `h` collapse / `l` expand the row under the cursor, `E` expand/collapse all.

## Mockup (100 cols, queue collapsed, one epic expanded)

```
┌┤ TASKS ├──────────────────────────────────────────────────────────────────────┐
│   bead                 recipe     status    stage      iter  tests    elapsed │
│   ▸ QUEUE              ·          7 ready · 3 blocked · next: alloy-o89.7     │
│   ▾ alloy-o89  Monitor TUI redesign        2 running · 1 judge · 4/9 done     │
│     ├─ alloy-o89.5     tdd-loop   judge     judge      3/5   0✗ 41✓  38m      │
│   ▶ ├─ alloy-o89.6     tdd-loop   running   tests      1/5   4✗ 0✓   6m       │
│     ├─ alloy-o89.7     tdd-loop   ready     ·          ·     ·        queued #1│
│     └─ ✓ 4 done  (o89.1 o89.2 o89.3 o89.4)                                    │
│   ▸ alloy-4ef  Project memory              1 running · 11/19 done             │
│     alloy-2ys          tdd-loop   running   implement  2/5   3✗ 12✓  14m      │
└───────────────────────────────────────────────────────────────────────────────┘
```
