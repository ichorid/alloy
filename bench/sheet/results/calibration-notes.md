# Calibration notes (Alloy c80a8fe, full x3, started 2026-11-07 12:13)

Live observations while the calibration runs; the numbers come from the reports.

## full-1 / toy-1: near-done work parked on max_total_checks (D1 + D3)
- Baseline and verifier ran one `pytest file::test` per test id: 7 checks a round,
  3 rounds = 21 checks in 3 minutes (limit 20).
- The final regression check (`pytest -q`) hit the long-red trap tests/test_units.py;
  the verifier spent a check confirming it was pre-existing (D3).
- The work was done (`tests/test_main.py`: 17 passed) but the run stopped on
  max_total_checks, which budget landing deliberately excludes. Everything in the
  roadmap depends on toy-1, so this copy is effectively over once it parks.
- Also: `python3 -m ruff` unavailable -> an unrunnable lint check spent budget.

## full-1 / toy-1: waiting-human dead end (12:16 → stopped 12:33)

- toy-1 stopped on max_total_checks (21 checks) with 17 tests passing: D1 + D3.
- `dispatch.park_after_human_minutes: 5` never fired. `Scheduler._maybe_park`
  runs only when the waiting run *holds* another ready top-level unit
  (`held_by` in `next_task`). Every other bead depends on toy-1, so nothing was
  ready and nothing was held. Parking would not have helped anyway (deferral
  keeps dependents blocked). With no operator the project is dead from the
  first needs-human on the critical path.
- Bench fix: `active_runs` no longer counts waiting-human, so a toy whose only
  run waits for a human ends as "queue empty" instead of idling to the 8 h
  deadline. full-1 of this campaign was stopped by hand at 12:33 to get the
  same effect (the campaign process runs the old code); outcome unaffected.
- Alloy finding (feeds step 3): a needs-human stop on the critical path blocks
  the entire roadmap; this is where "nearly done → land" (budget_landing does
  not cover max_total_checks) and classified exhaustion pay most.

## flaky-preexisting-1: flaky trap → blocking bug → remediation run (12:44)

- toy-21 (ranges) closed, but during it the implement role reported the flaky
  `tests/test_ids.py` failure seen in the required gate `python3 -m pytest -q`.
  Triage: "blocking (0.85) ... toy-21 cannot pass its check without this
  fixed". Alloy filed toy-889 [BUG] P1 and runs it as a separate bead: budget
  and wall time spent on a defect unrelated to the work (D3). No flaky rerun
  was attempted: a single rerun would have shown it green ~65% of the time.
- Note the gate itself was a *verifier-chosen* full-suite command marked
  required; the seed's units trap stays red, so a later bead will trip on it.

## full-3 interim oracle (12:5x): closed beads well below their areas (D8)

score 0.558. Closed: toy-4 ranges 0.50, toy-12 sheets 0.50, toy-13 names 0.57;
toy-2 arith 0.93 and toy-3 refs 0.92 regressed slightly after later beads (D4
candidates). Acceptance judges pass the one example in the acceptance line with
the agents' own narrow tests; the vague rest of the area is never pinned.

## flaky-preexisting ×2 (reports 20261107-1300-*)

| | run 1 | run 2 |
|---|---|---|
| oracle | 0.507 | 0.535 |
| trap bugs filed + remediated (D3) | 2 (ids, units) | 1 (units) |
| red checks whose only failures are traps | 10/46 | 9/42 |
| closed-but-failing ranges/logic/copy (D8) | 0.50/0.07/0.60 | 0.50/0.14/0.90 |
| needs-human, parks, D1 | 0 | 0 |
| claude cost / cursor tokens | $0.23 / ~1.0M | $0.25 / ~0.75M |

D3 reproduces 2/2 (traps edited by remediation beads, ~22% of checks red only
because of traps). D8 reproduces 2/2 and is the largest oracle loss: every
bead closes on its single acceptance example.
- Cost of D3 here is moderate: the parent beads did not fix the traps (no
  duplicated work); the "filed for the record" bug beads ran later as their
  own ready beads (toy-889 ids, toy-gm2/toy-c52 units). Implement iterations
  were barely affected (8 implement calls over 5 runs in run 1). Step 1's
  payoff on this bench may be small; weigh it against D8 (step 5) and the
  critical-path needs-human (step 3) when the full runs are in.

## affected-1: D4 trap avoided (13:12)

toy-31 changed `values.fmt` *and* switched csvio to a new `fmt_lossless`, adding
a csvio test (not weakening one). Probe green. Probable causes: the in-loop
verifier runs the whole suite (≈30% of checks in flaky runs were `regression`,
0.4 s each), and core's values.py docstring names csvio as fmt's other caller.
In Tentura the full suite is too slow for the loop; the toy's is not. If
affected-2 also avoids it: either make the bench's full suite slow (a slow
test module, no hint docstring) so the verifier skips it in-loop, or
downgrade step 2 to the docs part (done in ea7cad4).

## affected-1/toy-32: D3 → D1 oscillation (needs-human, max_iterations 5/5)

The CSV import itself was done and covered from iteration 2 on (acceptance said
so every time). The implementer had "fixed" the seed's units trap
(72.27 → 72.0) to get `python3 -m pytest -q` green; acceptance answered
`repair` three times in a row: "out-of-scope production change in
sheet/units.py". Make-the-suite-green vs stay-in-scope oscillated until the
iteration limit. Exactly the Tentura pattern: a pre-existing red test, no
pre-existing classification (step 1), and no detector for the same repair
reason repeating (step 3). On a critical path this stops the roadmap.

## Park deadlock in every targeted toy (fixed in 403494a)

stuck-1, stuck-2, affected-1, affected-2 all logged "parking failed" ~5 min
after their first needs-human: 13a0ff4's `git add -A` named the gitignored
`.alloy` in an exclude pathspec and git refused it. Engine.park had already
deferred the bead and cancelled the run, so the half-staged dirty checkout
held dispatch ("primary-checkout") for good, with no human asked. All four
toys were stopped by hand at 13:38 to get reports; their data up to the
deadlock stands, the rest of their roadmaps never ran. affected and stuck are
rerun at 403494a (stash_all: `add -u`, no excludes for ignored dirs; park
stashes before deferring/cancelling).

## affected ×2 and stuck ×2 at c80a8fe (partial: park deadlock, reports 20261107-1338-*)

| | affected-1 | affected-2 | stuck-1 | stuck-2 |
|---|---|---|---|---|
| oracle | 0.460 | 0.418 | 0.487 | 0.499 |
| needs-human | max_iterations (toy-32) | max_iterations (toy-31) | max_total_checks (toy-42) | max_total_checks (toy-42) |
| red checks only traps | 3/31 | 4/19 | 7/41 | 3/45 |
| trap bugs filed | 2 | 1 | 1 | 2 |
| D4 probe (test_csvio) | green, toy-31 closed | green, toy-31 parked | – | – |

- affected-2/toy-31: criterion met from iteration 2 on; first repair demanded
  reverting the units-trap "fix", later repairs chased the implementer's own
  new tests; max_iterations. With affected-1/toy-32: **D3→D1 oscillation 2/2**.
- D4 not reproduced 0/2 (the in-loop full suite catches the csvio coupling).
- stuck: toy-41 (perf) closed at oracle perf 0.75 in both; toy-42 (rows) ran
  out of checks 2/2 (D1).

Bench note: `seed_sha` in bench.json is HEAD after the "beads and tooling"
commit, which includes bd's random project id (`.beads/metadata.json`), so it
differs per copy. The root seed commit and the code trees are identical
(e.g. 6fb1cce for stuck; sheet/ e0ef5de, tests/ 4c1d3c3).
- full-2: park failed after toy-8's max_total_checks (~13:5x); stopped by hand; remaining open beads never ran.

## full ×3 at c80a8fe (reports 20261107-1352-full-*, regenerated with current run.py)

| | full-1 | full-2 | full-3 |
|---|---|---|---|
| oracle | 0.158 | 0.679 | 0.754 |
| beads closed / 15 | 0 | 12 | 15 |
| needs-human | max_total_checks (toy-1, critical path) | max_total_checks (toy-8) → park failed | none |
| checks (targeted/regression/lint) | 28 (26/1/1) | 213 (163/30/18) | 203 (133/45/16) |
| red checks only traps | 2 | 2 | 6 |
| agent calls / agent hours | 10 / 0.04 | 185 / 1.49 | 201 / 1.43 |
| claude $ / other tokens | 0.03 / 0.15M | 0.73 / 4.2M | 0.78 / 3.9M |

- Spread is enormous (0.16–0.75) and is decided by whether a needs-human
  lands on the critical path: D1 2/3 (both max_total_checks).
- D8 2/2 completed runs: logic 0.00/0.07, ranges 0.43/0.50, names 0.43/0.57,
  sheets 0.62/0.50, perf 0.50/0.50 -- closed on the one acceptance example.
- Early areas drift down after later beads (cells 0.9, arith 0.86, refs 0.92):
  mild D4.
- No D2, no D5, no duplicate bugs (D7) in any run.
- 403494a park verified live: stuck-1/toy-41 parked at 13:54 (patch + stash, tree clean). Park note had an empty reason (filed).

## affected ×2 at 403494a (reports 20261107-1400-affected-*)

| | affected-1 | affected-2 |
|---|---|---|
| oracle (format / csv) | 0.460 (1.0 / 1.0) | 0.460 (1.0 / 1.0) |
| roadmap | toy-31, toy-32 closed | toy-31 closed, toy-32 waiting-human |
| needs-human | – | max_total_checks (toy-32) |
| red checks only traps / trap bugs | 9/51, 1 | 3/50, 0 |
| D4 probe | green | green |

- affected-2/toy-32 stopped on max_total_checks while its uncommitted tree
  already scores csv 1.0 on the oracle: a "nearly done" stop like full-1/toy-1
  (17 tests green). budget_landing excludes max_total_checks, so finished
  work is thrown at a human. Feeds step 3 (classified exhaustion → land when
  nearly done) and the per-test-id check pattern (most checks are one test id).
- D4 0/4 across both affected campaigns → step 2 keeps only its docs part
  (ea7cad4); no gate-selection code.
- stuck-1 (403494a): toy-z3z was filed *by toy-cyy's own run* about the very defect toy-cyy fixes (triage: "the only bug filed" -- it does not count the run's own bead). z3z then ran after cyy closed and ended needs-human ("already fixed on HEAD"). Duplicate dedupe must include the current bead (D3/D7).
- stuck-1 (403494a)/toy-42: judge → human: "row-op work looks complete ... all 8
  tests plus the full suite green; the blocker is sheet/units.py (72.27→72.0)
  made to satisfy test_units". Third finished bead stopped by the units trap
  (with affected-1/toy-32 and affected-2/toy-31 at c80a8fe).
- stuck-1/toy-41: max_iterations; acceptance found a real hang on circular
  formulas in the new invalidation walk (legitimate repair loop).
