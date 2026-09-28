# Journal: operating Alloy against a real target project (tentura)

Everything that surprised the human-in-the-loop manager while running Alloy
day-to-day against `~/MY_SRC/tentura` -- not building Alloy itself (see
`journal.md` for that), but *operating* it: dispatching beads, resolving
`waiting-human` gates, reviewing diffs, landing work, and migrating the
project from per-bead worktrees to no-worktree mode mid-epic. One entry per
surprise, newest at the bottom. Each entry says what happened, why it
mattered, and what was done about it.

Status legend: **fixed** (root cause corrected), **mitigated** (Alloy now
surfaces or eases it, but part of the cause sits outside Alloy or still needs
an operator decision), **open** (nothing done about the root cause),
**external** (a target-repo issue, not Alloy's).

---

## 1. An orphaned `review-ready`/blocking bug bead silently stalls *all* dispatch forever -- fixed

A bug bead Alloy files during a remediation sub-run (`blocks_task: True`,
triage `blocking`) can sit at `review-ready` (or, worse, get its bd status
reset to `review-ready` after a failed land -- see #3) indefinitely if
whoever actually fixed the underlying issue didn't go through Alloy's own
resume/land cycle for *that specific run*. The scheduler's own dispatch rule
is working exactly as designed: "a review-ready bead holds its unit until it
lands, except the repair bug it's waiting on" -- but nothing ever prompts a
human to notice the bead is just *sitting* there. The result looks
identical to a scheduler hang: `alloy status` shows the process alive,
`bd ready` shows dozens of eligible beads, and nothing gets picked, poll
after poll, with no new log line because the block was already logged once
per process lifetime and then silently repeats.

This bit us at least eight separate times across one session
(`tentura-rhj`, `tentura-kv9`, `tentura-8i7`, `tentura-270`, `tentura-rlj`,
`tentura-sdl`, `tentura-52f`, and a stale-`IMPLEMENTING`-with-no-run variant
of the same thing) -- always the same shape: a bug was diagnosed correctly,
fixed correctly (often by resuming the *parent* bead directly with guidance,
skipping the child run's own resolution), and then left behind as an active
run record or unresolved bd status that nothing else ever re-checks.

**Diagnosis recipe**: when nothing dispatches and `alloy status --json`
looks calm, don't assume it's a code bug in the scheduler -- check
`bd list --status review-ready --json` and query the `runs` table directly
for rows with `status NOT IN ('done','failed','cancelled')`. Any hit is a
candidate holder.

**Fix once found**: `alloy cancel <bead-id>` to clear a stuck run record (or
`bd close <bug-id>` if it's a genuine bd-status mismatch), *after* confirming
the underlying issue is actually resolved -- otherwise you're just hiding a
real block.

**Suggested permanent fix**: the scheduler could periodically re-announce
(not just log-once) which bead is currently holding top-level dispatch, the
way `check_stalls` already announces a stalled *run*. A holder that's been
blocking for e.g. 30+ minutes with no active run of its own deserves the
same kind of surfaced warning a stalled run gets.

**Resolution (2026-09-27)**: The scheduler now re-announces a hold. `next_task` records which bead
holds top-level dispatch and why: a review-ready bead that is not landed or
is waiting on a named repair bug, a run waiting for a human, or a run whose
process is gone. The hold is logged when it starts. If a holder with no live
run of its own keeps dispatch waiting for `--stall-minutes`, the scheduler
logs a warning and emits a `stalled` event every `--stall-minutes` after
that. The event names the holder and points at `alloy reconcile <bead>`.

## 2. Land failure on a bead reverts its bd status to `review-ready` -- *even if a human already closed it* -- fixed

Manually `bd close`-ing a bead that Alloy still has land-state metadata for
(`alloy_land_state`, `alloy_run_id`, etc.) does not stick: the next time
Alloy's land-retry logic touches that bead (e.g. because its filed repair
bug just got closed, which is exactly what triggers a retry per the
documented mechanism), a failed land run overwrites bd status back to
`review-ready` regardless of the human's own close. This is *silent* --
`bd close` returns success, and the revert happens later, disconnected in
time from the action that caused it.

Concretely: a pure tracking epic (`tentura-617`, explicitly described in its
own body as "NOT an Alloy bead -- never run `alloy run` on it") had no diff
of its own to land. Alloy's own land-repair mechanism doesn't know that;
closing its filed repair bug (`tentura-3sn`) auto-retriggered a land attempt,
which failed identically (`red`: "the current diff is empty ... there is no
diff here to judge done/retry against"), filed a *new* repair bug
(`tentura-hfi`), and reset status to `review-ready` again. Closing *that*
bug would have retried a third time -- an unbounded loop, each iteration
burning a real Claude/cursor/codex call for a judge that can only ever say
the same thing.

**Fix**: clear `alloy_land_state` / `alloy_land_repair` / `alloy_run_id` /
`alloy_stage` / `alloy_worktree` / `alloy_branch` (and, if the bead was ever
mistakenly given one, `alloy_recipe`) directly via
`bd update --unset-metadata ...` *before* closing the last repair bug, then
re-`bd close` the original bead. Order matters: clearing metadata first
removes the retry's trigger condition.

**Suggested permanent fix**: the land-repair retry should check whether the
target bead is already `closed` before attempting another land, and should
recognize "this bead declares itself not an Alloy bead" (no `alloy_recipe`,
or an explicit marker) as a reason to never attempt landing it at all,
rather than discovering that fact fresh -- and expensively -- on every
retry.

**Resolution (2026-09-27)**: Landing no longer loops or reopens work. `Engine.land` refuses a closed
bead and a human-operated one (#5). A bead with nothing to land (a worktree
branch with no commits ahead of the target, or an in-place bead with no
recorded base) is closed without a land run. An in-place land now runs
against the base commit of the bead's first run, so the judge sees the real
diff instead of an empty one. That empty diff is what made every in-place
land of the tracking epic come back `red`. A failed land that finds the bead
closed meanwhile leaves it closed. `alloy_land_attempts` counts failed lands
in a row: at three the bead parks at waiting-human instead of filing another
repair bug. The scheduler's repair retry skips closed and manual beads.

## 3. `alloy cancel <bead>` on a bead the *live scheduler* currently owns kills the whole scheduler process -- fixed

Cancelling a bead that a background `alloy start` scheduler is actively
running (as opposed to a bead you started yourself with a standalone
`alloy run`) does not just stop that one run and let the scheduler keep
polling. The scheduler log shows `<bead>: stopping pid <scheduler-pid>`
followed by `stop requested; finishing the current run first` -- and then
the scheduler process itself exits. No further poll, no "scheduler down"
log line was seen before the process vanished from `pgrep`. Everything
downstream (bd closes, metadata edits, waiting for the scheduler to notice
ready work) looked like it should be working, but had no effect at all
because there was no scheduler left to notice anything -- indistinguishable
from a hang until you specifically check `pgrep -af "alloy.cli start"`.

Root cause not confirmed by reading source in this session (only observed
via logs), but the shape strongly suggests `alloy cancel` targets the same
signal/PID handle the scheduler uses for its own graceful-stop path, so
cancelling a scheduler-owned run escalates into (or is indistinguishable
from) a full `alloy stop`.

**Mitigation**: before cancelling a bead, check whether it's scheduler-owned
(`pgrep -af "alloy.cli start"` plus cross-referencing which bead the
scheduler's own log says it started). If it is, prefer `alloy stop`
(graceful, explicit) and manually re-run the bead afterward instead of
`alloy cancel`, or simply expect to need a scheduler restart right after.

**Suggested permanent fix**: `alloy cancel` should only ever signal the
specific run's own process group, and should log clearly (not silently) if
it also had to stop the parent scheduler as a side effect, so an operator
doesn't spend ten minutes debugging "nothing is dispatching" before checking
`pgrep`.

**Resolution (2026-09-27)**: Commit 0b3c273 first made `cancel` refuse a scheduler-owned run. Root
cause, confirmed in source: the scheduler runs beads in its own process, so
the recorded pid *is* the scheduler's pid. `alloy cancel` now writes the bead
to `.alloy/scheduler-cancel.json` and sends SIGUSR1. The scheduler cancels
only that run's task (the harness dies with it), books the run cancelled
and keeps serving. A scheduler-owned remediation child is refused, with the
parent bead to cancel instead. Cancelling a run also books any active
remediation children cancelled, and never reopens a bead a human closed.

## 4. Recipe changes only apply to beads with no `alloy_recipe` of their own -- mitigated

`alloy start --recipe X` (session-scoped) and the persistent
`alloy:default:recipe` project memory both only govern *unassigned* beads.
Every bead created by a planning/decomposition pass typically already
carries an explicit `alloy_recipe` in its bd metadata (e.g. baked in at `bd
create` time), which always wins over both the session flag and the
project-memory default. Switching recipes mid-project (e.g. moving from
`tdd-loop-sonnet` to `tdd-loop-sonnet-no-context` when switching worktree
modes) silently does nothing for the 30+ beads already queued unless you
also bulk-update each one's metadata (`bd update <id> --set-metadata
alloy_recipe=<new>` for every affected bead).

**Fix**: after any intended recipe switch, immediately check
`bd list --json` grouped by `metadata.alloy_recipe` to see how many beads
are still pinned to the old recipe, and bulk-update explicitly -- don't
assume the project-memory default covers existing work.

**Resolution (2026-09-27)**: This is still how defaults work, on purpose: a bead's own recipe wins.
It is now visible and cheap to act on. When a session or memory default is
set, the scheduler logs once how many ready beads pin another recipe. `alloy
assign-recipe <recipe> [--from R] [--apply]` shows, then writes, the bulk
retarget.

## 5. A bulk recipe-metadata update can accidentally arm epic/GATE(MANUAL) beads for autonomous dispatch -- fixed

Epics and human-operator "GATE (MANUAL)" checklist beads (e.g. "merge the
fact-history epic into main", explicitly described in their own body as "not
an Alloy bead, no alloy_recipe") sometimes still carry a stray
`alloy_recipe` field from an earlier bulk-decomposition or bulk-metadata
pass. There is no structural protection against this -- the scheduler's own
dispatch logic has no special case for "this bead's own text says never run
it," it only ever checks whether an `alloy_recipe` (explicit or default) is
present. Bulk-updating "every open bead's recipe" (per #4) can therefore
sweep up a bead that should categorically never be run autonomously, and the
scheduler will happily pick it up and start "implementing" a task whose
actual point was a human running a checklist -- in one case, a merge-gate
bead whose acceptance criteria were already satisfied by work already done,
spinning through several minutes of pointless "tests"-role activity before
being caught and cancelled.

**Mitigation**: before any bulk `alloy_recipe` update, exclude
epics/gates by type or label (`labels: manual`, `labels: merge-gate`,
`issue_type: epic`) rather than sweeping every "open" bead indiscriminately.

**Suggested permanent fix**: a bead whose description contains an explicit
"not an Alloy bead" / "never run alloy run on it" marker (or, better, a
first-class bd field/label for this) should be structurally excluded from
`next_task()`'s candidate pool regardless of what its metadata happens to
say, rather than relying on the convention "don't give it a recipe."

**Resolution (2026-09-27)**: `bead.manual` covers a `manual` or `merge-gate` label or `alloy_manual=true`.
Such beads are skipped by `next_task` and refused by `Engine.run` and
`Engine.land`, whatever `alloy_recipe` they carry. An epic that has children
is never picked as a task, even when all of them are closed: it only lands.
`alloy assign-recipe` never touches epics or manual beads, and `alloy
reconcile` flags (and with `--apply` removes) a recipe on a manual bead.

## 6. A required check command can omit steps the *actual* CI pipeline runs, so it can never pass as specified -- mitigated

The web-build verification check (`dart run tool/verify_web_version_consistency.dart`)
requires `build/web/app-assets/<version>/` artifacts and a populated
wasm-preload manifest, but the *required check's own command* only ran
`flutter build web` (missing `--wasm`) or, later, ran `--wasm` but skipped
`tool/apply_versioned_web_assets.dart` between `trim_web_deploy_artifact` and
`generate_wasm_preload_artifacts.dart` -- a step the project's real
`pipeline-prod.yml` runs but the bead-level check command didn't. Both times
this was independently rediscovered by a *different* bead as a fresh
"blocking bug," fixed by a human running the correct full pipeline by hand,
and then forgotten again because the fix was applied to *that run's
artifacts*, not to the check *definition* itself -- so the next bead to
touch anything web-build-related hits the identical wall from scratch.

**Fix (manual, per-incident)**: `flutter build web --wasm ...` then, in
order, `trim_web_deploy_artifact.dart` -> `apply_versioned_web_assets.dart`
-> `generate_wasm_preload_artifacts.dart` -> `verify_web_version_consistency.dart`.

**Suggested permanent fix**: the required check command lives in Alloy's
recipe/bead configuration, not the target repo, so a human fixing the
*artifacts* for one run can't fix it for the next one. This needs a genuine
recipe-config edit (outside what an operator can do bead-by-bead), and until
that happens every future web-touching bead in this project will keep
rediscovering the same "bug."

**Resolution (2026-09-27)**: The check definition now has a durable, human-owned home. The
`check-hints` memory key (no `alloy:` prefix, so runs and memory review never
rewrite it) holds commands an operator verified by hand. The verifier sees
them as "Operator-pinned checks (verified by a human)", ahead of every
unverified hint, with an instruction not to substitute a shorter variant.
Recording the full web pipeline there once replaces rediscovering it per
bead. The target repo's own CI config is still the source of truth, so this
is a mitigation.

## 7. A worktree-to-no-worktree architecture switch mid-epic silently breaks any test that assumed a separate branch existed -- mitigated

A release-gate test asserted `client pubspec minor > git show main:...
pubspec minor`, a valid invariant when each bead worked on an isolated
worktree/branch that hadn't yet merged into `main`. The moment the project
switched Alloy to no-worktree mode (every bead commits directly onto
`main`), the assertion degenerated to comparing `main` against itself --
since by the time the check runs, the bead's own version-bump commit is
already `HEAD` of `main`. The test started failing (or, worse, silently
tying) for every subsequent release-touching bead, and it was rediscovered
independently at least twice (once as a "blocking" nested-remediation find,
once as a separately-filed duplicate bug) before a human decided how to fix
it (drop the main-comparison half of the assertion; keep the still-valid
cache-buster-matches-pubspec half).

This isn't a bug in Alloy's code, but it's a real, structural consequence of
switching worktree modes that Alloy has no way to flag proactively -- there
is nothing that inspects the target repo's own test suite for "compares
against `main`" patterns before or after a mode switch.

**Suggested mitigation for future mode switches**: grep the target repo for
`git show main:` / `git diff main` / similar patterns in test code before
flipping worktree-mode defaults, and flag anything found for a human
decision up front rather than letting each hit be rediscovered piecemeal by
whichever bead happens to touch it next.

**Resolution (2026-09-27)**: An in-place run now tells the tests, implement, verifier, acceptance and
judge roles, and the critics' evidence, where the work lives. A `## Checkout`
section says the run is directly on branch `X`, with no separate bead branch,
so a test that compares against `X` compares the work with itself. The
target-repo test still needs a human decision. Grepping for `git show main:`
before a mode switch remains good practice.

## 8. The judge/acceptance role occasionally returns a literal glitched placeholder response -- fixed

One `claude:sonnet` judge call returned, verbatim,
`{"decision": "human", "reason": "test", "next_instructions": "test",
"confidence": 0.5}` -- a real, well-formed JSON response, but content that
is obviously not a genuine judgment (both free-text fields are literally the
word "test", confidence sitting exactly at the coin-flip midpoint). This
parses fine and gets treated as a real `human` verdict, parking the bead at
`waiting-human` with a nonsensical reason string that gives the operator no
actual signal about what's wrong -- because nothing *is* wrong; the model
call itself glitched.

**Mitigation**: when a `waiting-human` reason looks suspicious (a
one-word/placeholder-looking `reason`, especially matching common
prompt-injection-test tokens like "test"), independently re-verify the
bead's actual test/lint/build state before trusting the verdict, and resume
past it with an explicit note if everything is actually fine.

**Suggested permanent fix**: a lightweight sanity check on judge/acceptance
responses (e.g. reject and retry once if `reason` is under N characters or
matches a denylist of suspiciously generic strings) would catch this class
of glitch for free before it reaches a human.

**Resolution (2026-09-27)**: Acceptance and scope verdicts, and the judge, whose `reason` is a
stock filler word ("test", "todo", "placeholder", ...) are asked once more.
A second placeholder answer counts as a failed call: acceptance escalates,
scope rejects, and the judge parks at the human gate with a reason that
names the glitch.

## 9. The `tests` role can register throwaway `echo`/probe commands as permanent checks, permanently poisoning that run's baseline gate -- fixed

While debugging its own check-command syntax (in one case, using `dart
test`'s `-n` flag against `flutter test`, which doesn't support it), the
`tests` role used `echo <command>` to "preview" what it intended to run
instead of actually running the corrected command. Because *every* command
the tests role executes gets recorded by the harness as a real check, each
echo became a permanent, trivially-green (`echo` always exits 0) check tied
to that run's own history. The recipe's own "baseline unexpectedly green"
sanity gate (meant to catch tests that were supposed to start red) then
fires on that stale echo forever -- and it recurs on *every subsequent
resume of that same run_id*, no matter what guidance is given, because the
poisoned check is baked into that run's persisted history, not something a
`-m` resume message can retroactively un-register.

This happened three times in a row on the same bead before the pattern was
recognized: each resume attempt just produced a *new* throwaway echo check
with different placeholder text, and the same gate fired again.

**Fix**: don't keep resuming. `alloy cancel <bead-id>` (returns it to
`open`, discards the poisoned run) then a genuinely fresh `alloy run
<bead-id>` (new run_id, clean check history) resolved it immediately -- the
same bead implemented correctly and closed on the very first iteration of
the fresh run.

**Suggested permanent fix**: either don't record a check from a bare `echo`
invocation (or anything whose command doesn't touch the actual verify/test
tooling), or give the tests role an explicit "dry preview" capability that
doesn't get persisted as a check at all.

**Resolution (2026-09-27)**: `verify.noop_reason` refuses a lone `echo`/`printf`/`true`/`:`/`sleep`/
`exit`/`pwd`, even behind `VAR=value` prefixes. Compound shell expressions
still run. A refused check is recorded as not runnable ("not a check: ..."),
so the baseline reads "not runnable" instead of "unexpectedly green", and it
never lands in `alloy:check-hints`. A human resume into the tests stage now
resets the baseline repair budget. The old budget made the next bad baseline
park again at once. When the baseline itself was the problem, the resume
also drops the tests session that kept proposing it.

## 10. Nested remediation runs are not scope-constrained and can vastly over-build their fix -- mitigated

A remediation sub-run correctly diagnosed a broken widget-test finder (an
earlier UI refactor had removed the DOM structure the test searched for) and
wrote a correct fix -- but the actual diff spanned 9 files and ~622 lines,
including a full test suite *for the new test-helper itself*
(`beacon_cover_list_identity_finder_test.dart`, 186 lines) and a generic
Dart-source AST-body-extraction utility, none of which the underlying bug
needed. Alloy's own merge gate correctly rejected this as `too-broad`
(confidence split 0.55 too-broad / 0.44 merge -- a near-tie), which is
exactly what it's for, but the rejection discards the *whole* diff -- there
is no mechanism to keep the 2 files (~135 lines) that were actually needed
and drop the other 7. A human had to manually diff the rejected worktree,
identify the minimal necessary subset, and cherry-pick it directly into the
primary checkout.

**Suggested permanent fix**: on a `too-broad` merge-gate rejection, surface
the full file list with a per-file "needed / not needed" hint (the judge
that made the too-broad call presumably already has an opinion on which
files are core vs. incidental) so a human reviewer doesn't have to
re-derive that from scratch by reading every diff hunk.

**Resolution (2026-09-27)**: The scope verdict now carries `needed_files` and `incidental_files`.
A rejection's reason, which is noted on both beads, reads "Needed for the
fix: ...; not needed: ...", checked against the files the diff actually
touches. There is still no automatic trim: keeping part of a rejected fix
remains a human decision.

## 11. A discarded remediation worktree can leave a background server process squatting on a shared port -- fixed

A nested remediation started its own `tentura-server` (via
`scripts/run-server-local.sh`) bound to `:2080` from inside its own isolated
`.alloy/worktrees/<remediation-id>/` checkout, to run a local integration
test. When that remediation finished (successfully or not) and its worktree
was effectively abandoned, the server process kept running -- there is no
"stop background processes I started" step tied to a remediation run's
lifecycle. A *later*, unrelated run of the same web integration harness
then reused the already-listening port instead of starting its own server,
silently serving requests against a stale, discarded codebase and returning
wrong results (404s on a QA bootstrap route) that looked like a *new* bug
until traced back to the leftover process's cwd.

**Fix**: `ss -tlnp | grep <port>` to find the offending process, check its
`cmdline`/cwd for a `.alloy/worktrees/*` path that no longer corresponds to
active work, and kill it.

**Suggested permanent fix**: track any background process a run starts (PID
+ port) in that run's own record, and have worktree cleanup (on remediation
success *or* discard) kill anything still listed as owned by it.

**Resolution (2026-09-27)**: `WorktreeManager.stop_processes` finds processes whose cwd is inside an
isolated worktree (via /proc), sends SIGTERM, then SIGKILL after a grace
period. It runs when a remediation child finishes (any outcome), when a run
is cancelled and before a worktree is removed. It refuses the primary
checkout, where the operator's own tools live.

## 12. Isolated remediation worktrees can be missing generated codegen output entirely -- mitigated

A remediation's own isolated worktree failed to compile with "hundreds of
errors" like `The getter 'status' isn't defined for the type 'InboxItem'"`
-- not a real defect, just `dart run build_runner build -d` (Freezed/Ferry
codegen) never having been run in that specific fresh worktree before the
implementer tried to use types that only exist in generated `.freezed.dart`
files. Reported (correctly) by the implementer itself as non-blocking, but
it's worth flagging that a from-scratch remediation worktree does not
inherit codegen state the way a long-lived primary checkout does, and this
can look alarming (hundreds of compile errors) before you realize it's
purely a missing-build-step artifact of the worktree's freshness.

**Resolution (2026-09-27)**: A fresh worktree runs an executable `<repo>/.alloy/worktree-setup`
(for example `dart run build_runner build -d`), with `ALLOY_PRIMARY_CHECKOUT`
and `ALLOY_WORKTREE` set. A failure is logged and never blocks the run.
Target projects have to opt in by adding the hook.

## 13. Bug-bead lifecycle and Alloy's own run-tracking can drift independently, in both directions -- fixed

Across this session, `bd`'s bead status and Alloy's `runs` table
(status/stage/land-state) repeatedly ended up telling two different, wrong
stories about the same bead: bd said `closed` while Alloy's land-retry
reverted it to `review-ready` behind the scenes (#2); bd said `open` (after
`alloy cancel`, which resets a bead to ready regardless of whether a human
had already closed it out-of-band) even though the underlying issue was
already fixed and the bd close note said so; and a bead's own `run_id`
metadata pointed at a run row that had already terminated, with the bd
status field left stale at `IMPLEMENTING` indefinitely. None of this is
a single bug so much as a pattern: bd status, Alloy's run-state metadata on
the bead, and Alloy's own `runs` table are three places that can each say
something different about the same bead, and nothing reconciles them except
a human noticing the mismatch and manually editing one or more of the three.

**Mitigation**: after any manual bd status change on a bead Alloy has ever
run, or after any `alloy cancel`, re-check `bd show <id>` *and* the
`runs` table row for that bead before assuming the state is now consistent
-- don't trust either source alone.

**Suggested permanent fix**: a single `alloy repair-status <bead-id>`
command that reconciles bd status against the bead's latest run record (and
clears any stale `alloy_land_state`/`alloy_run_id`/`alloy_stage` metadata
that no longer corresponds to an active run) would have replaced most of
the manual SQLite queries and `bd update --unset-metadata` calls this
session needed.

**Resolution (2026-09-27)**: `alloy reconcile [<bead>] [--apply] [--json]` reports each mismatch
between bd status, the `alloy_*` metadata on the bead and the run ledger:

- a closed bead with a live run record
- a run marked running whose process is gone
- `implementing` or `waiting-human` in bd with no matching run
- a closed bead still carrying land state that can re-trigger a retry
- a missing repair bug
- a recipe on a manual bead

With `--apply` it fixes the mechanical ones. For a review-ready holder it
says what it is waiting for.

---

## 14. A run's agent process can die silently mid-stage, and the scheduler logs the stall but never auto-recovers -- open

Twice in a row on the same bead (`tentura-3dw`, a genuinely hard client-side
bug fix), the dispatched agent process vanished mid-run with no error in the
bead's own logs -- once mid-`implement` after ~90 minutes idle, once mid-land
`verify` after another ~20 minutes idle. Both times `scheduler.log` printed
exactly one line (`stalled: run process is gone`) and then went silent
*indefinitely* -- no automatic re-dispatch, no retry, nothing -- until a human
noticed the gap between "last log line" and wall-clock time and ran
`alloy resume`/`alloy land` by hand. `--stall-minutes 30.0` apparently governs
detection, not recovery. Each time, `alloy reconcile <bead>` correctly named
the exact problem (`run marked running but its process is gone`) but even
`--apply` only offers "book it cancelled and reopen" or "adopt it" -- it does
not do either automatically as part of normal scheduler operation.

Mitigation: when driving Alloy for a task that runs long (a full client test
suite as the land check, a hard bug needing many iterations), periodically
diff current wall-clock time against the last `scheduler.log` timestamp, not
just `alloy status`'s `elapsed`/`stage` fields -- those come from bead
metadata and stay frozen (looking "still running") long after the actual
process has died. If the gap is large with no matching log activity, don't
wait longer -- run `alloy reconcile <bead>` immediately, then
`alloy resume`/`alloy land`/`alloy run` to reattach. If the bead's real work
is already committed locally (check `git log` before assuming nothing
happened), it's often faster to finish the bead by hand (verify, push,
`bd close`, `alloy reconcile --apply`) than to keep re-dispatching into the
same stall.

---

## Summary: what actually costs the most operator time

In order of how much wall-clock time each pattern burned this session:

1. **Ghost dispatch blocks** (#1-#3): by far the most expensive class. Each
   incident looked identical to "the scheduler is hung" and took real
   investigation (SQLite queries against `alloy.db`, reading scheduler
   source) before the actual, simple cause (a bd/run-state mismatch) was
   found. A single reconciliation command (see #13's suggested fix) would
   have turned each of these into a 10-second check instead of a 10-30
   minute investigation.
2. **Recipe/metadata drift on mode switches** (#4, #5): entirely avoidable
   with a "what will this bulk update actually touch, and does it include
   anything with `issue_type: epic` or `labels: manual`" pre-check.
3. **Rediscovered-but-not-fixed check/test problems** (#6, #7): the
   underlying defect was diagnosed correctly every time by whichever bead
   hit it, but because the fix lives in a place individual beads can't edit
   (the recipe's own check command; a target-repo test file that isn't in
   scope for that bead), it kept coming back. These need a human to make a
   config-level or test-level fix *once*, out of band from any specific
   bead's run.
4. **Genuine model/harness flakiness** (#8, and the plain rate-limit /
   1200s-timeout fallbacks not written up as their own entries since they
   self-recovered cleanly every time): cheap to handle once recognized,
   but easy to mistake for a real defect on first sight.

