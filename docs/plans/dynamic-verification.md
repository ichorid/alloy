# Dynamic verification: agent-selected checks, deterministic execution, cheap acceptance gate

Source: operator request, 2026-09-22 (alloy-product-designer skill). One
epic, eight beads. Replaces fixed-command verification plus an overly broad
judge with: a repository-aware verifier that proposes checks, Alloy executing
every check itself, a cheap semantic acceptance gate, and escalation to the
existing judge/consilium/human path only when needed.

## Problem

Today the `tdd-loop` recipe assumes one canonical command per repository:

```text
ContextPacket.test_command -> resolve_command() -> one fixed command -> verify -> judge
```

- The judge is asked whether the task is done while the only hard fact it
  has is an exit code Alloy already knows. A red suite costs a judge call to
  rediscover that the implementation is incomplete.
- The fixed command needs `alloy_test_cmd`, `verify.command` or `AUTODETECT`
  to be right for every repo, and evolves with each project.
- Verification scope cannot change per iteration: one targeted test while
  iterating, an affected-module suite once it is green, a lint or analyzer
  pass, a regression suite before completion.
- The baseline "prove the tests are red" step runs the whole suite, so an
  unrelated pre-existing failure looks like a valid red baseline.

## Target architecture

Three responsibilities, kept apart:

```text
WHAT SHOULD BE CHECKED?        HOW IS IT EXECUTED?          IS THERE ENOUGH EVIDENCE?
verifier agent (repo-aware) -> Alloy runs the command   -> acceptance gate (Jev, cheap)
proposes one check             exit code, output, log      accept / verify_more / repair / escalate
                                                            escalate -> judge -> guard (existing)
```

Alloy knows how to run a shell command, capture exit code and output,
persist the evidence under the run's log dir, and enforce limits. Alloy does
not know how any particular project is tested. Adding Alloy to a repository
must not require a project-specific adapter.

### Revised `tdd-loop` graph

```text
context -> estimate -> tests -> prove_red -> implement -> (triage/remediate as today)
                        ^          |                           |
                        |   green: repair tests               v
                        +----------+                     verification_loop
                                                          |          ^
                            required check RED -> implement (repair) |
                                                          |          |
                                        verifier stop -> acceptance_gate
                                                     accept | verify_more | repair | escalate
                                                       |         |            |         |
                                                     guard  verification  implement   judge -> guard
                                                              _loop
```

`guard` stays the only place that decides whether the loop continues. The
consilium, human gate, abort and remediation paths are unchanged.

### Models (new, in `alloy/models.py`)

```python
CHECK_KINDS = ("targeted", "affected", "regression", "ui", "integration",
               "lint", "typecheck", "build", "custom")

class CheckRequest(BaseModel):
    command: str
    purpose: str = ""
    kind: str = "custom"          # one of CHECK_KINDS; unknown -> "custom"
    required: bool = True

class CheckResult(BaseModel):
    command: str
    purpose: str = ""
    kind: str = "custom"
    required: bool = True
    exit_code: int
    duration_s: float = 0.0
    timed_out: bool = False
    output_tail: str = ""
    log_path: str | None = None
    passed: int | None = None     # parsed counts when a known runner is recognised
    failed: int | None = None

    ok        -> exit_code == 0 and not timed_out
    runnable  -> not timed_out and exit_code != 127
    headline() -> "exit 0" | "3 passed, 1 failed" | "timed out after 61s" | "command not found"

class VerifierAction(BaseModel):     # verifier role output
    action: Literal["run", "stop"]
    command: str = ""                 # run
    purpose: str = ""
    kind: str = "custom"
    required: bool = True
    reason: str = ""                  # stop
    remaining_risks: list[str] = []

class TestsOutput(BaseModel):         # tests role output
    summary: str = ""
    baseline_checks: list[CheckRequest] = []

ACCEPTANCE_DECISIONS = ("accept", "verify_more", "repair", "escalate")
class AcceptanceVerdict(BaseModel):   # `decision` first: Jev classifies on the first enum
    decision: Literal[...]
    reason: str = ""
    confidence: float = 0.0
```

`TestReport` is replaced by `CheckResult`. `Attempt.tests` becomes
`Attempt.checks` (a one-line summary such as `3 checks, all green` or
`RED: flutter test test/x_test.dart (exit 1)`); the rendered history line
reads `checks:` instead of `tests:`.

### Config (`alloy/config.py`)

```yaml
verification:
  max_checks_per_iteration: 5      # verifier run-actions between two implement calls
  max_total_checks: 20             # across the whole run (all budget windows)
  max_command_timeout_minutes: 15  # per check; a verifier cannot ask for more
  max_baseline_repairs: 2          # tests-role re-runs when the baseline is green
  min_acceptance_confidence: 0.6   # below this an `accept` becomes `escalate`
```

`VerificationSpec` replaces `VerifySpec`. A legacy `verify:` block is still
parsed: `timeout_minutes` maps onto `max_command_timeout_minutes`; `command`
is accepted with a logged deprecation warning and ignored by the graph.

### State (`TddState`, additions and removals)

| key | change |
| --- | --- |
| `test_command` | removed |
| `baseline` | now `list[CheckResult]` (one per baseline check) |
| `baseline_checks` | new, `list[CheckRequest]` from the tests role |
| `baseline_repairs` | new, int |
| `tests_session` | new, `{"runner": str, "session_id": str} | None` |
| `checks` | new, `Annotated[list[CheckResult dict], operator.add]`, every check this run executed |
| `iteration_checks` | new, int, verifier run-actions since the last implement |
| `verifier_stop` | new, `{"reason": str, "remaining_risks": list[str]} | None` |
| `acceptance` | new, `AcceptanceVerdict dict | None` |
| `last_tests` | removed; `last_check` (the most recent `CheckResult`) replaces it for prompts and the human-gate payload |

Everything else (attempts, bugs, remediations, complexity, consilium, human
gate fields) is untouched.

### Runtime (`alloy/runtime.py`)

- `RunContext.run_check(request: CheckRequest) -> CheckResult` replaces
  `RunContext.verify(command)`. Timeout is
  `verification.max_command_timeout_minutes`. Log file:
  `check-<index>-<kind>-<epoch ms>.log` under the run's log dir, header
  `$ <command>` / `purpose=` / `kind=` / `exit=`.
- `RunContext.call(..., resume_session: str | None = None)` forwards the
  session id to runners whose `run` declares the keyword (same
  `_accepts_kwarg` pattern as `effort`).
- `set_tests_summary` keeps its name and column (monitor/CLI display); its
  argument is the last check headline plus a count, e.g. `4 checks, last: exit 0`.

### Runner session resume (`alloy/runners/*`)

`run(..., resume_session: str | None = None)`:

| runner | argv change |
| --- | --- |
| `claude`, `claude-write` | `--resume <id>` before `-p` |
| `codex`, `codex-readonly`, `astra` | `exec resume <id>` instead of `exec` (flags unchanged) |
| `cursor`, `cursor-plan` | `--resume <id>` |
| generic | `{resume_args}` template token, empty when no session |
| `jev` | ignored (stateless) |

Confirm each CLI's actual flag with `--help` before pinning; the tests assert
the adapter's argv, never the CLI.

This is how "pause the test-writing agent, do not stop it" is realised: a
CLI harness cannot be held open across graph nodes and checkpoints, so the
same session is resumed with its context and provider-side cache instead.
When the session cannot be resumed (different runner after a fallback, no
session id, resume exits non-zero) the role runs fresh with the compact
packet and the ledger row says `resumed=false`.

### Prompts

- **context**: `test_command` leaves `CONTEXT_SCHEMA`; `check_hints:
  list[str]` (optional) replaces it: commands the repo's files, scripts and
  CI config suggest. `detect_command()` output is appended to the hints as
  a bootstrap fallback, never executed on its own.
- **tests**: structured output `TestsOutput`. The prompt asks for
  `baseline_checks`: the exact commands that demonstrate the new behaviour is
  not implemented yet, targeted at the files it wrote.
- **implement**: `## Checks that must go green` (the baseline checks) replaces
  `## Test command`. On a repair iteration it also receives `## Failed check`
  (command, exit code, output tail), `## Current diff` (clipped) and
  `## Previous repair instructions`.
- **verifier** (new role): receives task brief, acceptance criteria, context
  packet, current diff (clipped), changed files, every previous
  `CheckResult` of this run (headlines plus the last tail), iteration number,
  checks remaining in this iteration and in the run, compact attempt
  history. Never a transcript. Returns `VerifierAction`.
- **acceptance** (new role, Jev primary): receives acceptance criteria,
  current diff, the list of test files changed since the tests stage, every
  check headline of this iteration, the verifier's stop reason and
  remaining risks. Returns `AcceptanceVerdict`.
- **judge**: unchanged wording, now reached only via `escalate`; its
  `## Test results` section renders the checks of this iteration.

### Deterministic rules the graph enforces

1. A **required** check with `exit_code != 0` routes to `implement` with
   repair instructions. No acceptance call, no judge call.
2. A check that is **not runnable** (exit 127 or timeout) is never treated as
   a red baseline and never as a passing check; the verifier is told and asked
   for another command. Two unrunnable checks in a row inside one iteration
   count as a verifier failure and park at the human gate.
3. `prove_red` accepts a baseline only when every baseline check is runnable
   and red. Green or missing baseline checks re-run the tests role with an
   explicit instruction, at most `max_baseline_repairs` times, then the
   human gate with reason `baseline unexpectedly green`.
4. The verifier may issue at most `max_checks_per_iteration` run-actions
   between two implement calls and `max_total_checks` in the run. Hitting
   the per-iteration limit forces a stop with `remaining_risks +=
   ["verifier check budget exhausted"]`; hitting the total limit is a guard
   breach (`max_total_checks reached`) and parks at the human gate exactly
   like `max_iterations`.
5. `accept` still passes through `guard`, which refuses `done` when the last
   required check of the iteration is red or the diff is empty.
6. `accept` with `confidence < min_acceptance_confidence`, a failed
   acceptance call, or `escalate` all route to the judge.
7. `verify_more` when the iteration's check budget is already exhausted
   becomes `escalate`.
8. Every verifier and acceptance call is an agent call and counts toward
   `max_agent_calls`; wall time and iterations apply as before.

## Beads

Dependency order (critical path 1 -> 3 -> 4 -> 5 -> 7):

1. **Check primitives and verification config** -- `CheckRequest`,
   `CheckResult`, `run_check`, `VerificationSpec`, `RunContext.run_check`.
   No graph change.
2. **Runner session resume** -- `resume_session` through the runner
   protocol, every adapter, `RunContext.call`, the fake harness. Independent.
3. **Tests role emits baseline checks; `prove_red` node** -- structured
   tests output, targeted baseline executed by Alloy, explicit green
   handling, implement prompt shows the checks. Depends on 1.
4. **Verifier role and `verification_loop` node** -- dynamic per-iteration
   checks, red-required-check to repair without the judge, hard check
   limits, project-agnostic execution, `checks` evidence in state. The judge
   still runs after a verifier stop in this bead. Depends on 3.
5. **Acceptance gate** -- Jev `accept/verify_more/repair/escalate`,
   confidence threshold, escalation to the judge. Depends on 4.
6. **Session continuity** -- test-repair and verifier calls resume the
   tests-writer's session; fresh-call fallback. Depends on 2 and 4.
7. **Remove the fixed-command path and document the new flow** --
   `ContextPacket.test_command`, `resolve_command`, `VerifySpec.command`,
   `alloy_test_cmd` demoted to an optional hint, `TestReport` alias gone;
   AGENTS.md, README, recipe YAML comments, the product-designer skill.
   Depends on 5 and 6.
8. **Checkpoint/resume inside the loop; checks in status and monitor** --
   a run killed mid-loop resumes without re-running finished checks;
   `alloy status --json` exposes `checks`. Depends on 4.

## Relationship to existing beads (reviewed 2026-09-22)

- `alloy-5wb.2` (focused tests, then the full suite before done) was
  superseded by bead 4: the verifier chooses the focused command while
  iterating and a broader suite before stopping.
- `alloy-5wb.3` now feeds the acceptance gate first (bead 5) and refines its
  first-cut `changed_tests` with content fingerprints; blocked by bead 5.
- `alloy-5wb.1` (junk-free, per-file clipped diff) now covers every
  diff-rendering site: judge, acceptance, verifier, implement repair,
  evidence packet; blocked by bead 5.
- `alloy-5wb.4` (timed auto-resume) also carries `retry_at` out of the
  verifier and acceptance failure paths.
- `alloy-5wb.6` (stdin prompts) must honour `resume_session`; blocked by
  bead 2.
- `alloy-4ef.4` (prompts.assemble) now lists the verifier and acceptance
  prompts and a check-hints run layer; blocked by bead 7 so it refactors the
  final prompt set once. `alloy-4ef.1`, `.6`, `.7`, `.21` had wording
  updated (VerificationSpec, `_render_checks`, verifier prompt gets memory,
  `alloy:check-hints`).
- `alloy-4ef.9` was re-scoped from "persist the verified test command" to
  "persist the verifier's runnable check commands as `alloy:check-hints`";
  blocked by bead 7.
- `alloy-w9d` (monitor limits) shares snapshot key sets with bead 8; both
  extend rather than replace them.
