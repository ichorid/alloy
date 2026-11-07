# Step 9: `full` ×3 at d3093ff against calibration (2026-11-07)

Calibration: c80a8fe, reports `20261107-1352-full-*` (full-1..3). Final: d3093ff (steps 1, 3, 5, 7),
reports `20261107-2004-full-*` (full-4..6). Same seed, recipe `toy-cheap`; no deadline hit; transcript audits clean.

| | calibration (full-1 / 2 / 3) | d3093ff (full-4 / 5 / 6) |
|---|---|---|
| oracle | 0.158 / 0.679 / 0.754 (mean 0.530) | 0.663 / **0.853** / 0.158 (mean 0.558) |
| roadmap beads closed (of 15) | 0 / 12 / 15 | 12 / 15 / 1 |
| needs-human | 1 / 1 / 0 (all `max_total_checks`) | 3 / 0 / 1 (judge-human ×2, `max_iterations` ×2) |
| checks | 28 / 213 / 203 | 177 / 156 / 60 |
| agent calls, claude $ | 10 $0.03 / 185 $0.73 / 201 $0.78 | 251 $0.93 / 231 $0.84 / 97 $0.33 |
| duplicate trap bugs | – | 0 / 0 / 1 (units test filed twice under different titles) |

Per area, runs that got past the first bead (calibration full-2/3 against d3093ff full-4/5):

| area | calibration | d3093ff |
|---|---|---|
| logic | 0.00 / 0.07 | **1.00 / 1.00** |
| ranges | 0.43 / 0.50 | 0.93 / 0.57 |
| refs | 0.92 / 0.92 | 1.00 / 1.00 |
| sheets | 0.62 / 0.50 | 0.25 / 0.88 |
| errors | 0.86 / 0.57 | 0.29 (parked) / 0.86 |
| copy, rows | 0.90, 0.70 / 1.00, 1.00 | 0.00, 0.00 (parked) / 0.90, 1.00 |
| undo | 0.12 / 0.62 | 0.12 / 0.50 |

## Where each d3093ff run stopped

- **full-6 / toy-2 (arith)**, `nearly-done` at `max_iterations`: every criterion green, acceptance kept asking
  for an OverflowError fix the criteria never named; 12 dependents blocked. Same failure shape as calibration
  full-1 (first bead parked, oracle 0.158). **Fixed in 69ae08a**: a repair with `criteria_met` budget-lands and
  files the extra request as a follow-up bug.
- **full-4 / toy-8 (rows)**, judge-human at the check cap: the regression's two red tests were the excused
  traps ("every failure not this task's -- flaky/pre-existing"), and Alloy counted the check green, but the
  judge refused to "waive the gate". The judge and acceptance prompts do not say what an excused failure means.
- **full-4 / toy-7 (copy)**, judge-human: `refine` added `$`-anchor and cross-sheet criteria; composer did not
  implement `$` and rewrote the tests to fit the code. A model limit made visible by the stricter criteria;
  the stop itself was correct.
- **full-4 / toy-5 (errors)**, `progressing` at `max_iterations`: a real stop (regressions introduced).

## Reading

- The total oracle is still dominated by whether the first beads land: one run per side stops on the
  first bead. Mean 0.530 → 0.558 is inside the noise.
- Where runs got going, the vague areas moved: logic 0–0.07 → 1.0 (2/2), ranges and refs up, and full-5 closed
  all 15 roadmap beads at 0.853, above any calibration run.
- Trap handling is clean: one bug per trap per run, no trap-driven stops; no check-cap stops (calibration: 2).
- The new stops are judgement stops at the human gate, not budget stops. Two of the four are Alloy defects
  (toy-2, fixed; toy-8, open).
