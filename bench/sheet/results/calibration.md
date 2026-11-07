# Calibration: the sheet bench on current Alloy (2026-11-07)

Alloy refs: **c80a8fe** (full ×3, flaky-preexisting ×2) and **403494a**
(affected ×2, stuck ×2: c80a8fe plus the park fix, see "Invalidations").
Recipes `toy-cheap` (composer-2.5 writes, haiku@low judges, cheap fallbacks
only). Per-run reports: `20261107-*.json`; running notes: `calibration-notes.md`.

## Reproduction of the Tentura defect classes

A class counts as reproduced at ≥2/3 `full` runs or 2/2 targeted runs.

| Class | Evidence | Reproduced | Step |
|---|---|---|---|
| D1 budget exhaustion → needs-human | full 2/3 (max_total_checks ×2); affected 3/4 (max_iterations ×2, max_total_checks ×1); stuck: toy-42 stopped 4/4 (max_total_checks ×3, judge-human on the units trap ×1), toy-41 1/4 (max_iterations, a real cycle hang) | **yes** | 3 |
| D1a "nearly done" stop | full-1/toy-1 (17 tests green), affected-2/toy-32 (tree scores csv 1.0) stopped on max_total_checks; `budget_landing` excludes that limit | **yes** | 3 |
| D2 "more verification" at the limit | 0 in every run | no | 4 → drop |
| D3 flaky / pre-existing failures in the loop | trap-only red checks in 13/13 runs (2–20 per run, up to 25% of checks); trap bugs filed and remediated in 10/13; **finished work stopped by the trap** 3× (the implementer "fixes" the units trap, acceptance/judge demand a revert: affected 2/2 at c80a8fe → max_iterations, stuck-1/toy-42 at 403494a → judge-human) | **yes** | 1 |
| D4 regression reaches done | csvio probe green 4/4; early areas drift only slightly in full (cells 0.9, arith 0.86) | no | 2 → docs only |
| D5 baseline trouble | 0 | no | 4/5 → drop the baseline part |
| D6 queue held at the human gate | full-1: needs-human on the critical path, auto-park cannot help (nothing else ready); park deadlock at c80a8fe in 5/5 runs that parked (fixed in 403494a: 4 live parks succeeded in stuck) | **yes** | 3 (+ fixed bug) |
| D7 same mistake repeated / duplicate bugs | duplicate-title bugs 0; a remediation run filed a bug for its own defect, which later ran on a fixed tree and ended needs-human (stuck-1, alloy-fw6) | weak | 6 (data-driven); dedupe → 1 |
| D8 vague spec, closed but failing | every completed run: logic 0.00–0.14, ranges 0.43–0.50, names 0.43–0.57, sheets 0.50–0.62, perf 0.50–0.75 on closed beads | **yes**, largest oracle loss | 5 |

## Noise band (run-to-run spread)

| Metric | full (n=3) | flaky-preexisting (n=2) | affected 403494a (n=2) | stuck 403494a (n=2) |
|---|---|---|---|---|
| oracle | 0.158 / 0.679 / 0.754 | 0.507 / 0.535 | 0.460 / 0.460 | 0.457 / 0.476 |
| beads closed | 0 / 12 / 15 of 15 | 3/3, 3/3 (+1–2 bug beads) | 2/2, 1/2 | 1/3, 2/3 (+ bug beads) |
| needs-human | 1 / 1 / 0 | 0 / 0 | 0 / 1 | 3 / 1 |
| checks | 28 / 213 / 203 | 46 / 42 | 51 / 50 | 81 / 63 |
| agent calls | 10 / 185 / 201 | 65 / 69 | – | 104 / 81 |
| claude $ / other tokens | 0.03 / 0.73 / 0.78 ; 0.15M / 4.2M / 3.9M | 0.23 / 0.25 ; 1.0M / 0.75M | – | 0.38 / 0.30 ; 1.9M / 1.2M |

`full` is dominated by where the first needs-human lands: the oracle spans
0.16–0.75 at the same ref. Comparisons on `full` therefore use the per-area
scores of beads that ran and the needs-human/park counts, not the total
alone; a total-score delta needs ≥3 runs per side.

## Invalidations and bench fixes during calibration

- Calibration v1 (before c80a8fe): Alloy's venv leaked into toy checks
  (no pytest) → all runs discarded; bench runs the frozen venv binary directly
  (alloy-kgp tracks the Alloy-side fix).
- c80a8fe: park failed on a gitignored `.alloy` ("paths are ignored") after
  deferring and cancelling → dirty checkout held dispatch for good. The four
  targeted toys and full-2 were stopped by hand at the deadlock; data up to
  it stands. Fixed in 403494a (stash `add -u`, no excludes for ignored dirs,
  stash before defer/cancel); affected and stuck rerun at 403494a.
- `wait` treated waiting-human as active, so a toy whose only run waits for a
  human idled to the deadline (full-1 stopped by hand; fixed in 2d53597).
- The report scored the oracle with Alloy's venv interpreter (3.13) while the
  toy runs system `python3` (3.14): stuck-2's working tree scored 0.000
  (eager annotation error) instead of 0.476. Fixed (oracle uses the toy's
  python3); every live toy re-reported, only stuck-2 changed. The partial
  c80a8fe affected/stuck reports (`20261107-1338-*`) were scored on 3.13 and
  their toys are gone: not comparable.
- Transcript audit: no run touched `bench/`, the oracle or the reference.

## Decisions for the plan

1. **Step 1 (flaky + pre-existing in loop): keep, high priority.** The
   oscillation and the trap remediations are the costliest repeated pattern;
   add "dedupe against the run's own bead and recently closed bugs"
   (alloy-fw6).
2. **Step 2: docs part only** (done in ea7cad4); D4 did not reproduce.
3. **Step 3: keep, highest priority.** max_total_checks drives most D1, often
   on finished work; classify the stop, let a "nearly done" check-budget stop
   land like `budget_landing`, and look at why most checks are single test
   ids (per-test targeted checks burn the 20-check budget).
4. **Step 4: drop** (no D2, no D5).
5. **Step 5: keep** — D8 is the largest oracle loss.
6. **Step 6: downgrade** to data-driven (D7 weak).
7. **Step 7: keep** (small).
8. **Step 8: drop** unless later campaigns show a dead task (none seen).
