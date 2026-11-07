# Steps 1, 3, 5 (and 7) on the sheet bench (2026-11-07)

Refs:
- **calibration**: c80a8fe for flaky-preexisting, 403494a for affected and stuck (`calibration.md`);
- **step 1**: 3504892 (flaky and pre-existing failures excused in the loop, triage dedupe);
- **steps 1+3**: 59048ab (spent check budget reads the evidence, classified limit stops, stall consilium, per-file checks);
- **all**: d3093ff (+3bd8c33 verifier told what is excused, +0b51c64 evaluator warning, +d3093ff refine role).

Recipe `toy-cheap`: composer-2.5 writes, haiku@low judges. At d3093ff the recipe enables `refine` and
`verification.rerun_tests`. Each column has two runs. Reports are `20261107-{1450,1451,1508,1658,1717,1729}-*.json`;
"partial" reports are from runs cut short by the memory incident (below), with their roadmap beads already decided.

## stuck (beads beyond the models: perf, rows, sheets)

| | calibration | step 1 | all |
|---|---|---|---|
| roadmap beads closed | 1/3, 2/3 | 2/3, 2/3 | **3/3, 3/3** |
| needs-human | 3, 1 | 1, 1 (max_total_checks) | **0, 0** |
| oracle | 0.457 / 0.476 | 0.499 / 0.484 | **0.546 / 0.542** |
| rows / sheets / perf | 0, 0 / 0.62, 0.5 / 0.25, 0.75 | 0.8, 0 / 0.5, 0.62 / 0.25, 0.75 | **0.8, 0.8 / 0.75, 0.75 / 0.75, 0.75** |
| checks per run | 81 / 63 | 96 / 78 | **50 / 52** |
| agent calls, claude $ | 104 $0.38 / 81 $0.30 | 80 $0.31 / 54 $0.18 | 68 $0.27 / 84 $0.32 |

toy-42 (insert/delete rows) stopped in 4 of 4 calibration runs; it closed in both runs at d3093ff. Both step-1
stops were verifier-side `max_total_checks`, which is the case step 3 handles.

## flaky-preexisting (vague ranges / logic / copy beads, both traps present)

| | calibration | step 1 | all |
|---|---|---|---|
| roadmap beads closed | 3/3, 3/3 | 3/3, 3/3 | 3/3, 3/3 |
| needs-human | 0, 0 | 0, 0 | 0, 0 |
| logic | 0.07 / 0.14 | 0.14 / 0.21 | **1.00 / 1.00** |
| ranges | 0.50 / 0.50 | 0.50 / 0.50 | **0.57 / 0.93** |
| copy | 0.60 / 0.90 | 0.60 / 0.60 | 0.60 / 0.60 |
| oracle | 0.507 / 0.535 | 0.516 / 0.520 | **0.578 / 0.601** |
| alloy-bugs per trap | trap bugs filed + remediated | exactly 1 per trap | exactly 1 per trap |

The logic jump comes from `refine`. toy-22's acceptance ("IF with a comparison picks the right branch") gained
criteria for every comparison, AND/OR/NOT, `&`, LEN, UPPER/LOWER, LEFT/RIGHT/MID and case-insensitive names. The
acceptance gate then checked each one ("Every refined criterion has a passing test"). Copy did not move: its
oracle failures are semantics the refined criteria also missed (relative/absolute mixes beyond `$A1`).

## affected (a core change that can break CSV)

| | calibration 403494a | steps 1+3 | all |
|---|---|---|---|
| roadmap beads closed | 2/2, 1/2 | 2/2, 2/2 | 2/2, 2/2 |
| needs-human | 0, 1 (max_total_checks on finished work) | 0, 0 | 0, 0 |
| oracle | 0.460 / 0.460 | 0.449 / 0.460 | 0.460 / 0.460 |
| checks per run | 51 / 50 | 34 / 50 | 38 / 57 |

The oracle cannot move here: the scenario's areas are already at 1.0 on every closed tree. The gain is in
done-rate: the "nearly done" stop of calibration affected-2/toy-32 did not recur.

## Reading

- **Step 1** removed trap-driven stops and duplicate trap bugs. On its own it moved stuck from 1–2 to 2 closed
  beads per run. The remaining stops were all on the check cap.
- **Step 3** removed the check-cap stops (0 needs-human in 6 runs at 59048ab/d3093ff, against 4 in 6 before) and
  halved the checks per stuck run (per-file baseline and verifier commands).
- **Step 5** gave the largest oracle gain on a vague bead (logic 0.07–0.21 → 1.0 in 2/2). Steps 3 and 5 were
  measured together on stuck and flaky, so their separate share there is not isolated.
- Cost per run stayed within the calibration range.
- n = 2 per cell. stuck done-rate, needs-human and logic lie far outside the calibration spread; copy and
  affected oracle do not move.

## Incident: a recursive test ate the machine's memory

The step-1 flaky-1 remediation of a PYTHONPATH bug added a test that runs the whole suite from inside the
suite. The recursive pytest took ~43 GB, and every campaign driver was killed. The fix is in bench abb984a:
- each toy's Alloy runs in its own systemd scope under alloy-bench.slice (MemoryMax 8G, TasksMax 768);
- toy claude agents run with `--strict-mcp-config`;
- drivers run in detached scopes.

At d3093ff the cap fired once: a stuck-3 remediation bead peaked at 6.3 GB, and systemd-oomd killed that toy's
whole scope, scheduler included. The roadmap beads were already closed. alloy-jdp tracks a per-check cap in
Alloy, so that only the runaway check fails.
