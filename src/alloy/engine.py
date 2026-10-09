"""Starting, resuming and finishing one task's workflow.

The engine is the only place that knows how Beads status, the worktree, the run
ledger and the LangGraph checkpoint fit together -- and it keeps them consistent
whether a run ends normally, pauses for a human, or dies mid-flight.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.types import Command

from alloy import beads as bd
from alloy import recipes
from alloy.beads import Bead, BeadsClient
from alloy.checkpoints import has_pending_interrupt, open_checkpointer, read_checkpoint
from alloy.config import ConfigError, RecipeConfig, load_recipe
from alloy.events import EVENT_BUDGET_LANDED
from alloy.models import Outcome
from alloy.outbox import deliver_pending
from alloy.paths import AlloyPaths
from alloy.procs import pid_alive, read_pid, terminate_group, terminate_pid
from alloy.runners import RunnerRegistry
from alloy.runtime import RunContext
from alloy.sandbox import RunSandbox, SandboxError, SandboxSpec
from alloy.sandbox import activate as sandbox_activate
from alloy.sandbox import write_status as write_sandbox_status
from alloy.store import (
    RUN_CANCELLED,
    RUN_CLAIMING,
    RUN_DONE,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_WAITING_HUMAN,
    ActiveRunExists,
    Store,
)
from alloy.worktree import Worktree, WorktreeError, WorktreeManager

RECURSION_LIMIT = 200

log = logging.getLogger("alloy.engine")


class EngineError(RuntimeError):
    pass


@dataclass
class RunResult:
    bead_id: str
    run_id: str
    outcome: str
    reason: str = ""
    interrupt: dict[str, Any] | None = None
    worktree: str | None = None

    @property
    def paused(self) -> bool:
        return self.outcome == Outcome.WAITING_HUMAN.value


@dataclass
class Engine:
    repo: Path
    paths: AlloyPaths
    store: Store
    beads: BeadsClient

    @classmethod
    def open(cls, repo: Path | str, root: Path | str | None = None) -> "Engine":
        repo = Path(repo).resolve()
        paths = AlloyPaths.resolve(root, project=repo).ensure()
        return cls(
            repo=repo,
            paths=paths,
            store=Store(paths.alloy_db),
            beads=BeadsClient(repo=repo),
        )

    # -- public API -------------------------------------------------------

    async def run(self, bead_id: str, *, recipe_name: str | None = None) -> RunResult:
        """Start a fresh run, or continue one that was interrupted by a crash."""
        # Land whatever this store already committed for the bead before
        # deciding anything from bd's state: a run that failed locally while
        # bd was unreachable must read as `failed` here, not as an
        # `implementing` bead free for a takeover.
        deliver_pending(self.store, self.beads, bead_id=bead_id)
        # Every dead `claiming` row, not just the latest: with several
        # crashed claimants, resolving only the newest can expose another
        # unresolved one as "latest", which neither `_refuse_if_live` (its
        # pid is dead) nor `_resumable_run` (it is not `running`) stops.
        self._resolve_dead_claims(bead_id)
        bead = self.beads.show(bead_id)
        self._refuse_if_any_live(bead_id)
        existing = self._resumable_run(bead_id)
        if existing is not None:
            log.info("%s: adopting orphaned run %s", bead_id, existing["run_id"])
            # Nothing ran between the owner's last write and now; that dead
            # time must not eat the wall-time budget (see Store.mark_resumed).
            if not existing.get("paused_at"):
                self.store.update_run(existing["run_id"], paused_at=existing["updated_at"])
            return await self._execute(
                bead,
                existing["recipe"],
                run_id=existing["run_id"],
                thread_id=existing["thread_id"],
                resume_payload=None,
                expect={"status": existing["status"], "pid": existing.get("pid")},
            )

        if bead.manual:
            raise EngineError(
                f"{bead_id} is marked human-operated (label manual/merge-gate or "
                f"{bd.META_MANUAL}); Alloy does not run it"
            )
        name = recipe_name or bead.recipe
        if not name:
            raise EngineError(
                f"{bead_id} has no recipe; set one with `bd update {bead_id} --set-metadata {bd.META_RECIPE}=tdd-loop`"
            )
        self.validate_recipe(name)
        if bead.status not in (bd.STATUS_READY, bd.STATUS_IMPLEMENTING):
            raise EngineError(f"{bead_id} is '{bead.status}'; only '{bd.STATUS_READY}' beads can start")

        run_id, reverts_to, bead = self._claim(bead, name)
        try:
            return await self._execute(bead, name, run_id=run_id, thread_id=run_id, resume_payload=None, fresh=True)
        except EngineError as exc:
            # Failed before any real work started (worktree, config): hand the
            # bead back so it is offered again instead of looking busy forever
            # -- and always settle the row, or a long-lived scheduler's own
            # pid would keep it "running" (and its concurrency slot taken).
            outbox: list[tuple[str, dict[str, Any]]] = []
            if reverts_to == bd.STATUS_READY:
                outbox = [("status", {"status": bd.STATUS_READY, "if_status": bd.STATUS_IMPLEMENTING})]
            if self.store.finish_run(
                run_id,
                status=RUN_CANCELLED,
                outcome=Outcome.CANCELLED.value,
                reason=str(exc),
                bead_id=bead_id,
                outbox=outbox,
                expect=self._owned(),
            ):
                deliver_pending(self.store, self.beads, bead_id=bead_id)
            raise

    def _claim(self, bead: Bead, name: str) -> tuple[str, str, Bead]:
        """Create this run's `claiming` row and claim the bead in bd: the new
        run_id, the status to revert bd to on an early failure, and the bead
        as bd says it is now."""
        bead_id = bead.id
        run_id = uuid.uuid4().hex
        # The row is created before bd is ever touched: a crash between this
        # and the claim leaves a `claiming` row with nothing in bd yet; a
        # crash between the claim and promoting to `running` leaves a row bd
        # already agrees the bead is claimed for. Both are resolved by
        # `_resolve_stuck_claiming`. `exclusive` makes this store the local
        # arbiter: a second claimant here is refused before it can reach bd.
        try:
            self.store.create_claiming_run(
                run_id=run_id,
                bead_id=bead_id,
                thread_id=run_id,
                recipe=name,
                repo=self.repo,
                worktree=None,
                branch=None,
                log_dir=None,
                exclusive=True,
            )
        except ActiveRunExists as exc:
            raise EngineError(f"{bead_id}: {exc}") from exc
        # A bead already `implementing` with no live row (a takeover: its
        # owner's row is terminal or gone) is claimed the same way, CAS'd on
        # `implementing` -- that stamps *this* run as bd's `alloy_run_id`,
        # which is what fences off any stale outbox row the old owner still
        # has pending (see `alloy.outbox`).
        try:
            claimed = self.beads.claim(bead_id, expect=bead.status, run_id=run_id)
        except bd.BeadsError:
            # Unknown whether the claim landed (a timeout can still have
            # applied it). Clearing the pid hands the row to the stuck-claim
            # sweep, which asks bd -- otherwise a long-lived scheduler's own
            # pid would keep this row "live", and the bead wedged, forever.
            self.store.update_run(run_id, expect={"status": RUN_CLAIMING}, pid=None)
            raise
        if not claimed:
            self.store.discard_claiming_run(run_id)
            raise EngineError(f"{bead_id} was claimed by someone else")
        if not self.store.promote_claiming_run(run_id):
            # Lost to a concurrent cancel of this very run_id between the
            # claim landing in bd and this promotion: the row is already
            # booked cancelled (and bd already reverted), so there is
            # nothing left to execute.
            raise EngineError(f"{bead_id}: run {run_id} was cancelled before it could start")
        reverts_to = bead.status
        # What bd says now: the snapshot above predates the claim. Without
        # this, dispatch would enqueue a stale `implementing if <old status>`
        # write that could re-claim a bead someone reopened meanwhile.
        bead = bead.model_copy(
            update={"status": bd.STATUS_IMPLEMENTING, "metadata": {**bead.metadata, bd.META_RUN_ID: run_id}}
        )
        return run_id, reverts_to, bead

    def _refuse_if_superseded(self, bead: Bead, run_id: str, expect: dict[str, Any] | None) -> None:
        """The synchronous half of the outbox's run-id fence, for a run being
        resumed or adopted: if bd was claimed by another run since this one
        last owned it, continuing would execute beside the new owner with
        every bd write of ours silently fenced off. Book it superseded (no bd
        write: bd is not ours to touch), or it would be offered for adoption
        forever."""
        owner = bead.metadata.get(bd.META_RUN_ID)
        if owner and owner != run_id:
            self.store.finish_run(
                run_id,
                status=RUN_CANCELLED,
                outcome=Outcome.CANCELLED.value,
                reason=f"superseded: bd now belongs to run {owner}",
                expect=expect,
            )
            raise EngineError(f"{bead.id}: bd now belongs to run {owner}, not {run_id}; not continuing it")

    def _owned(self) -> dict[str, Any]:
        """The run-row CAS a process executing a run holds: still `running`,
        still recorded as ours. A concurrent cancel or adopter that moved the
        row first makes every later write of ours lose instead of clobber."""
        return {"status": RUN_RUNNING, "pid": os.getpid()}

    def _resolve_dead_claims(self, bead_id: str) -> None:
        for record in self.store.runs_for_bead(bead_id):
            if record["status"] == RUN_CLAIMING and not pid_alive(record.get("pid")):
                self._resolve_stuck_claiming(record)

    def _resolve_stuck_claiming(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """A `claiming` row survived a crash with its owning process gone.

        If bd never saw *this* claim, there is nothing to adopt: discard the
        row and let the bead be offered fresh. If bd shows it `implementing`
        with `alloy_run_id` metadata matching this row's run_id -- stamped
        atomically with the claim itself, see `BeadsClient.claim` -- the claim
        did land: promote the row to `running` (filling in the
        worktree/branch/log_dir a crash this early never recorded) so the
        ordinary orphan-adoption path in `run()`/`Scheduler.recover()` picks
        it up exactly like any other crash survivor.

        Checking `alloy_run_id`, not just the status, matters: two racing
        claimants each create their own `claiming` row before either calls
        `claim()`. Only one's claim can land in bd, but *both* rows could
        independently crash before learning the outcome. Bd's status alone
        can't tell which row is the real winner; the metadata bd's own CAS
        update recorded can.

        A transient read failure (bd itself unreachable) must never discard
        the row -- that would be indistinguishable from "the claim never
        landed" and strand the real winner. Only bd genuinely answering
        "not this one" does.
        """
        bead_id = record["bead_id"]
        run_id = record["run_id"]
        bead = self.beads.show(bead_id)  # BeadsError propagates: unknown stays unresolved, not discarded
        if bead.status != bd.STATUS_IMPLEMENTING or bead.metadata.get(bd.META_RUN_ID) != run_id:
            self.store.discard_claiming_run(run_id)  # a no-op unless the row is still `claiming`
            return self.store.get_run(run_id)
        worktrees = WorktreeManager(repo=self.repo, root=self.paths.worktrees)
        # The same CAS as the claim-time promotion: a concurrent cancel (or
        # another resolver) that already moved the row on wins, rather than
        # being silently overwritten back to `running`.
        self.store.promote_claiming_run(
            run_id,
            worktree=str(self.repo),
            branch=worktrees.current_branch() or "HEAD",
            log_dir=str(self.paths.run_logs(run_id)),
            # `_execute`'s own later repair (see its non-fresh branch) only
            # fires when `worktree` is still NULL -- filling that in above
            # without also recording `base_commit` here would leave it NULL
            # forever, letting a later resume pick a different HEAD as this
            # run's diff base.
            base_commit=worktrees.head(self.repo),
        )
        return self.store.get_run(run_id)

    async def resume(self, bead_id: str, instructions: str = "") -> RunResult:
        """Continue a paused or crashed run, optionally answering the human gate."""
        record = self.store.latest_run_for_bead(bead_id)
        if record is None:
            raise EngineError(f"no run recorded for {bead_id}")
        if record["status"] in (RUN_DONE, RUN_CANCELLED):
            raise EngineError(f"run for {bead_id} already finished ({record['status']})")
        if record["status"] == RUN_CLAIMING and not pid_alive(record.get("pid")):
            # Same resolution `run()` applies: without it, `resume` could be
            # pointed at a losing claimant's row (bd's status alone can't
            # tell winner from loser) and promote/continue it regardless.
            record = self._resolve_stuck_claiming(record)
            if record is None:
                raise EngineError(f"{bead_id}: that claim never landed in bd; nothing to resume")
            if record["status"] == RUN_CANCELLED:
                raise EngineError(f"run for {bead_id} already finished ({record['status']})")
        self._refuse_if_any_live(bead_id)
        bead = self.beads.show(bead_id)
        pending = has_pending_interrupt(self.paths.workflows_db, record["thread_id"])
        if record.get("retry_at"):
            self.store.update_run(record["run_id"], retry_at=None)
        return await self._execute(
            bead,
            record["recipe"],
            run_id=record["run_id"],
            thread_id=record["thread_id"],
            resume_payload={"instructions": instructions} if pending else None,
            expect={"status": record["status"], "pid": record.get("pid")},
        )

    def cancel(self, bead_id: str, *, grace_s: float = 5.0, scheduler_timeout_s: float = 60.0) -> bool:
        """Stop the run -- the process too, not just the bookkeeping.

        A run the live scheduler executes in-process records the scheduler's
        own pid, so signalling that pid would take down the whole scheduler.
        Instead the scheduler is asked (SIGUSR1 + `scheduler_cancel`) to
        cancel just that run; it books the run cancelled itself and keeps
        serving. A remediation child the scheduler is running is refused:
        cancel its parent bead.
        """
        for _ in range(3):
            record = self.store.latest_run_for_bead(bead_id)
            if record is None or record["status"] in (RUN_DONE, RUN_FAILED, RUN_CANCELLED):
                return False
            pid = record.get("pid")
            if record["status"] in (RUN_RUNNING, RUN_CLAIMING) and pid_alive(pid) and pid != os.getpid():
                scheduler_pid = read_pid(self.paths.scheduler_pid)
                if scheduler_pid is not None and int(pid) == scheduler_pid:
                    return self._cancel_via_scheduler(bead_id, record, scheduler_pid, timeout_s=scheduler_timeout_s)
                log.info("%s: stopping pid %s", bead_id, pid)
                if not terminate_pid(int(pid), grace_s=grace_s):
                    raise EngineError(f"could not stop pid {pid} running {bead_id}")
            if self._cancel_bookkeeping(bead_id, record, grace_s=grace_s):
                return True
            # The row moved between reading it and booking it (an adopter
            # took a dead run over, or the run settled): look again -- a new
            # live owner gets stopped, a terminal row means nothing to cancel.
            log.info("%s: run %s changed under cancel; re-reading", bead_id, record["run_id"])
        raise EngineError(f"{bead_id}: the run kept changing under cancel; try again")

    def _cancel_via_scheduler(
        self, bead_id: str, record: dict[str, Any], scheduler_pid: int, *, timeout_s: float
    ) -> bool:
        if record.get("parent_run_id"):
            parent = self.store.get_run(record["parent_run_id"]) or {}
            raise EngineError(
                f"{bead_id}: run {record['run_id']} is a remediation child the scheduler is running "
                f"for {parent.get('bead_id') or record['parent_run_id']}; cancel that bead instead"
            )
        path = self.paths.scheduler_cancel
        path.write_text(json.dumps({"bead": bead_id, "run": record["run_id"]}), encoding="utf-8")
        log.info("%s: asking the scheduler (pid %s) to cancel run %s", bead_id, scheduler_pid, record["run_id"])
        try:
            os.kill(scheduler_pid, signal.SIGUSR1)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                current = self.store.get_run(record["run_id"]) or {}
                if current.get("status") in (RUN_DONE, RUN_FAILED, RUN_CANCELLED):
                    return current["status"] == RUN_CANCELLED
                time.sleep(0.2)
        except ProcessLookupError:
            pass
        finally:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        raise EngineError(
            f"{bead_id}: the scheduler (pid {scheduler_pid}) did not cancel run {record['run_id']} "
            f"within {timeout_s:.0f}s; see {self.paths.scheduler_log}, or `alloy stop --now`"
        )

    def _cancel_bookkeeping(self, bead_id: str, record: dict[str, Any], *, grace_s: float) -> bool:
        """Book a stopped run cancelled: its orphaned harnesses, its still-active
        remediation children, and the bead (back to ready unless a human
        closed it meanwhile -- a cancel must never reopen finished work)."""
        for child in self.store.children_of(record["run_id"]):
            if child["status"] in (RUN_RUNNING, RUN_WAITING_HUMAN, RUN_CLAIMING):
                self._cancel_bookkeeping(child["bead_id"], child, grace_s=grace_s)
        # journal 9: a run that died without cleanup (SIGKILL, OOM) leaves its
        # harness process group running; the inflight row remembers its pid.
        for call in self.store.active_calls(record["run_id"]):
            harness_pid = call.get("pid")
            if harness_pid and pid_alive(harness_pid):
                log.info("%s: stopping orphaned harness pid %s", bead_id, harness_pid)
                terminate_group(int(harness_pid), grace_s=grace_s)
            self.store.discard_call(call["call_id"])
        if record.get("worktree"):
            WorktreeManager(repo=self.repo, root=self.paths.worktrees).stop_processes(Path(record["worktree"]))
        # The note depends on bd's current status (a best-effort read; None
        # when bd is unreachable). The status revert never does: it is
        # decided at *delivery* time, CAS'd on whichever of the states this
        # run itself could have left bd in -- so a human's close, or any
        # other status, is never overwritten, even when this read failed
        # (an unreadable status must not degrade to an unguarded write), and
        # the outbox's run-id fence drops it outright if a newer run has
        # claimed the bead by the time it is delivered.
        current_bd_status = self._bead_status(bead_id)
        if current_bd_status == bd.STATUS_DONE:
            outbox: list[tuple[str, dict[str, Any]]] = [
                ("note", {"text": f"alloy: run {record['run_id']} cancelled; the bead stays closed"})
            ]
        else:
            outbox = [
                (
                    "status",
                    {"status": bd.STATUS_READY, "if_status_in": [bd.STATUS_IMPLEMENTING, bd.STATUS_WAITING_HUMAN]},
                ),
                (
                    "note",
                    {"text": f"alloy: run {record['run_id']} cancelled; worktree left at {record['worktree']}"},
                ),
            ]
        if not self.store.finish_run(
            record["run_id"],
            status=RUN_CANCELLED,
            outcome=Outcome.CANCELLED.value,
            reason="cancelled by operator",
            bead_id=bead_id,
            outbox=outbox,
            # Only the row this cancel read and stopped: an adopter that
            # took it over meanwhile (new pid) or a settle that finished it
            # wins, and `cancel()` re-reads.
            expect={"status": record["status"], "pid": record.get("pid")},
        ):
            return False
        deliver_pending(self.store, self.beads, bead_id=bead_id)
        return True

    def _bead_status(self, bead_id: str) -> str | None:
        try:
            return self.beads.show(bead_id).status
        except bd.BeadsError:
            return None

    def validate_recipe(self, name: str) -> RecipeConfig:
        """Both halves of a recipe must exist: the YAML and the graph builder."""
        try:
            recipes.get(name)
            return self.load_config(name)
        except (KeyError, ConfigError) as exc:
            raise EngineError(str(exc)) from exc

    # -- epics --------------------------------------------------------------

    def close_finished_epic(self, bead_id: str) -> RunResult:
        """Close a tracking epic once every descendant has closed.

        Every bead now commits directly on the checked-out branch and closes
        itself as soon as its own final check is green (see `tdd_loop.py`'s
        `final_check` stage) -- an epic never runs and has nothing of its own
        to merge; it is just the parent record that groups its children.
        """
        bead = self.beads.show(bead_id)
        if bead.status == bd.STATUS_DONE:
            raise EngineError(f"{bead_id} is already closed")
        if bead.manual:
            raise EngineError(
                f"{bead_id} is marked human-operated (label manual/merge-gate or {bd.META_MANUAL}); "
                "Alloy does not close it"
            )
        if bead.issue_type != "epic":
            raise EngineError(f"{bead_id} is not an epic")
        open_descendants = self.beads.open_descendants(bead_id)
        if open_descendants:
            names = ", ".join(b.id for b in open_descendants)
            raise EngineError(f"{bead_id} cannot close: it still has open descendants: {names}")
        self.beads.note(bead_id, "alloy: all descendants closed; closing the tracking epic")
        self.beads.close(bead_id)
        log.info("%s: closed (all descendants done)", bead_id)
        return RunResult(bead_id, "", Outcome.DONE.value, reason="all descendants closed")

    def _refuse_if_live(self, bead_id: str, record: dict[str, Any] | None) -> None:
        # `claiming` counts as live too: a second process racing in while the
        # first has already won bd's CAS but not yet promoted its row to
        # `running` must not be let through to dispatch a second execution --
        # bd's own status alone can't tell "mid-claim" from "stuck claim" (see
        # `_resolve_stuck_claiming`), but a live pid here proves it's neither.
        if (
            record is not None
            and record["status"] in (RUN_RUNNING, RUN_CLAIMING)
            and pid_alive(record.get("pid"))
            and record.get("pid") != os.getpid()
        ):
            raise EngineError(
                f"{bead_id} is already being {'claimed' if record['status'] == RUN_CLAIMING else 'run'} "
                f"by pid {record['pid']} (run {record['run_id']}); `alloy cancel {bead_id}` stops it"
            )

    def _refuse_if_any_live(self, bead_id: str) -> None:
        """`_refuse_if_live` over every row still owning the bead, not just
        the latest: a live winner's row can sit behind newer dead ones."""
        for record in self.store.runs_for_bead(bead_id):
            self._refuse_if_live(bead_id, record)

    # -- graph plumbing ---------------------------------------------------

    def load_config(self, name: str) -> RecipeConfig:
        return load_recipe(name, alloy_root=self.paths.shared_root, project=self.repo)

    def build_context(
        self,
        bead: Bead,
        recipe_name: str,
        *,
        run_id: str,
        checkpointer: Any,
        worktree: Worktree | None = None,
        base_commit: str | None = None,
        fresh: bool = False,
    ) -> RunContext:
        config = self.load_config(recipe_name)
        worktrees = WorktreeManager(repo=self.repo, root=self.paths.worktrees)
        if worktree is None:
            # Every bead runs directly in the primary checkout, on whatever
            # branch is already checked out there -- no isolated worktree, no
            # separate bead branch.
            worktree = Worktree(bead.id, self.repo, "", base_commit or worktrees.head(self.repo))
            # `run()` always creates this run's own row before calling here,
            # so on a true first-ever dispatch this run_id is the only row
            # for the bead. A resume/orphan-adopt (`not fresh`) or a fresh
            # retry after a past attempt (a *different*, prior run_id already
            # on record) both mean dirty state here is this bead's own work,
            # not something foreign to refuse on.
            retrying_own_work = not fresh or any(r["run_id"] != run_id for r in self.store.runs_for_bead(bead.id))
            if not base_commit and not retrying_own_work and worktrees.has_uncommitted_tracked_changes(self.repo):
                # commit_wip does `git add -A`: on a truly fresh in-place
                # start (never run before, not a resume/retry of this
                # bead's own prior attempt) a pre-existing modified/staged
                # tracked file would be silently swept into this bead's
                # diff and commit. Refuse instead of mixing it in.
                # Untracked clutter (e.g. `.beads/`) is left alone -- it's
                # normal checkout noise, not someone's in-progress edit.
                raise WorktreeError(
                    f"primary checkout {self.repo} has uncommitted tracked changes; "
                    f"commit or stash them before running {bead.id}"
                )
        log_dir = self.paths.run_logs(run_id)
        log_dir.mkdir(parents=True, exist_ok=True)
        branch = worktrees.current_branch() or "HEAD"
        checkout_note = (
            f"This run works in place: directly in the primary checkout, on branch `{branch}`, "
            "not on a separate bead branch. Commits for this task land on "
            f"`{branch}` itself, so a check or test that compares the tree or HEAD with "
            f"`{branch}` (`git show {branch}:...`, `git diff {branch}`) compares this work "
            "with itself."
        )
        ctx = RunContext(
            bead=bead,
            recipe=config,
            run_id=run_id,
            worktree=worktree,
            worktrees=worktrees,
            registry=RunnerRegistry(config.runners, log_dir=log_dir),
            store=self.store,
            checkpointer=checkpointer,
            log_dir=log_dir,
            beads=self.beads,
            checkout_note=checkout_note,
            limit_state=self.limit_state_reader(),
        )
        return ctx

    def limit_state_reader(self):
        """harness -> its rate-limit state from local files, for the runner
        breaker (`alloy.limits.breaker.read_state`)."""
        from functools import partial

        from alloy.limits.breaker import read_state

        return partial(read_state, cache_path=self.paths.limits_cache)

    # -- internals --------------------------------------------------------

    async def _execute(
        self,
        bead: Bead,
        recipe_name: str,
        *,
        run_id: str,
        resume_payload: dict[str, Any] | None,
        thread_id: str | None = None,
        worktree: Worktree | None = None,
        parent_run_id: str | None = None,
        fresh: bool = False,
        expect: dict[str, Any] | None = None,
    ) -> RunResult:
        """`fresh=True` means `run_id` already names a `running` row `run()`
        created and populated with bd's claim before calling here (see
        `Engine.run`); this only fills in the worktree/branch/log_dir/
        base_commit a claim-time row cannot know yet. `fresh=False` (resuming
        an existing run, orphaned or paused) never touches those -- they were
        already recorded by this same run's own earlier fresh pass.

        `expect` (required unless `fresh`) is the `(status, pid)` the caller
        read and judged dead/paused: taking the row over is CAS'd on it, so
        two adopters of the same orphan can never both execute it."""
        assert fresh or expect is not None, "adopting/resuming a run needs the (status, pid) it was judged on"
        recipe = recipes.get(recipe_name)
        # One graph thread per run, not per bead: re-running a cancelled bead
        # must start from an empty graph, not inherit the abandoned one.
        thread_id = thread_id or run_id

        async with open_checkpointer(self.paths.workflows_db) as checkpointer:
            recorded_base = None
            prior = None
            if not fresh:
                prior = self.store.get_run(run_id)
                recorded_base = prior.get("base_commit") if prior else None
            try:
                ctx = self.build_context(
                    bead,
                    recipe_name,
                    run_id=run_id,
                    checkpointer=checkpointer,
                    worktree=worktree,
                    base_commit=recorded_base,
                    fresh=fresh,
                )
            except (ConfigError, KeyError, WorktreeError) as exc:
                raise EngineError(str(exc)) from exc

            # Before dispatch is recorded: a refused sandbox (`mode: bwrap` on
            # a host without bwrap) must not take an adopted row over first.
            try:
                sandbox = self._start_sandbox(ctx, run_id)
            except SandboxError as exc:
                raise EngineError(f"{bead.id}: {exc}") from exc
            try:
                final = await self._invoke(
                    ctx,
                    bead,
                    recipe,
                    recipe_name,
                    sandbox,
                    run_id=run_id,
                    thread_id=thread_id,
                    prior=prior,
                    fresh=fresh,
                    expect=expect,
                    parent_run_id=parent_run_id,
                    resume_payload=resume_payload,
                )
            finally:
                self._stop_sandbox(sandbox)

            result = self._settle(ctx, bead, recipe_name, run_id, final)
            log.info("%s: run %s -> %s %s", bead.id, run_id, result.outcome, result.reason)
            return result

    async def _invoke(
        self,
        ctx: RunContext,
        bead: Bead,
        recipe: Any,
        recipe_name: str,
        sandbox: RunSandbox,
        *,
        run_id: str,
        thread_id: str,
        prior: dict[str, Any] | None,
        fresh: bool,
        expect: dict[str, Any] | None,
        parent_run_id: str | None,
        resume_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Take the run row, then run the graph inside `sandbox`."""
        self._record_dispatch(
            bead,
            recipe_name,
            ctx,
            run_id=run_id,
            prior=prior,
            fresh=fresh,
            expect=expect,
            parent_run_id=parent_run_id,
        )
        deliver_pending(self.store, self.beads, bead_id=bead.id)
        graph = recipe.build_graph(ctx)
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": RECURSION_LIMIT,
        }

        if resume_payload is not None:
            payload: Any = Command(resume=resume_payload)
        elif fresh or read_checkpoint(self.paths.workflows_db, thread_id) is None:
            # A run that died before its first checkpoint has nothing to
            # continue from; LangGraph would refuse an empty input.
            payload = recipe.initial_state(ctx)
        else:
            payload = None  # continue from the last checkpoint

        try:
            with sandbox_activate(sandbox):
                return await graph.ainvoke(payload, config)
        except Exception as exc:
            # A cancellation is not an Exception, so an interrupted process
            # leaves the run marked running -- which is what makes it
            # recoverable later. Only a genuine error fails the task here.
            log.exception("%s: run %s crashed", bead.id, run_id)
            if self.store.finish_run(
                run_id,
                status=RUN_FAILED,
                outcome=Outcome.FAILED.value,
                reason=str(exc),
                bead_id=bead.id,
                outbox=[
                    ("status", {"status": bd.STATUS_FAILED, "if_status": bd.STATUS_IMPLEMENTING}),
                    ("note", {"text": f"alloy: run {run_id} crashed: {exc}. Worktree kept at {ctx.worktree.path}"}),
                ],
                expect=self._owned(),
            ):
                deliver_pending(self.store, self.beads, bead_id=bead.id)
            else:
                self._log_lost_settle(bead.id, run_id, "failed")
            raise
        except BaseException:
            log.warning("%s: run %s interrupted; it stays resumable", bead.id, run_id)
            raise

    def _start_sandbox(self, ctx: RunContext, run_id: str) -> RunSandbox:
        """The run's private-/tmp namespace (alloy.sandbox); `off` is a no-op.
        Started before dispatch is recorded, so a refusal (`mode: bwrap` on
        a host without bwrap) never takes an adopted row over."""
        spec = getattr(ctx.recipe, "sandbox", None) or SandboxSpec()
        own_paths = (ctx.worktree.path, ctx.worktrees.repo, ctx.log_dir, self.paths.root)
        sandbox = RunSandbox(run_id=run_id, spec=spec, extra_share=tuple(str(p) for p in own_paths)).start()
        if sandbox.kind != "off":
            write_sandbox_status(self.paths.root, run=sandbox.info())
        return sandbox

    def _stop_sandbox(self, sandbox: RunSandbox) -> None:
        try:
            sandbox.stop()
        except Exception:
            log.warning("could not stop the sandbox of run %s", sandbox.run_id, exc_info=True)
        if sandbox.kind != "off":
            write_sandbox_status(self.paths.root, drop_run=sandbox.run_id)

    def _record_dispatch(
        self,
        bead: Bead,
        recipe_name: str,
        ctx: RunContext,
        *,
        run_id: str,
        prior: dict[str, Any] | None,
        fresh: bool,
        expect: dict[str, Any] | None,
        parent_run_id: str | None,
    ) -> None:
        """Take (or keep) the run row for this process and enqueue the
        dispatch-time bd writes in the same commit -- CAS'd, so a cancel or a
        rival adopter that moved the row first makes this raise instead."""
        dispatch_metadata = {
            bd.META_RUN_ID: run_id,
            bd.META_WORKTREE: str(ctx.worktree.path),
            bd.META_BRANCH: ctx.worktree.branch,
            bd.META_RECIPE: recipe_name,
        }
        dispatch_outbox: list[tuple[str, dict[str, Any]]] = [("metadata", {"values": dispatch_metadata})]
        if bead.status != bd.STATUS_IMPLEMENTING:
            dispatch_outbox.append(("status", {"status": bd.STATUS_IMPLEMENTING, "if_status": bead.status}))

        if fresh:
            # The row already exists (`run()` created it, running, before
            # ever calling here) -- this only fills in what a claim-time
            # row cannot know yet.
            taken = self.store.update_run(
                run_id,
                bead_id=bead.id,
                outbox=dispatch_outbox,
                worktree=str(ctx.worktree.path),
                branch=ctx.worktree.branch,
                log_dir=str(ctx.log_dir),
                parent_run_id=parent_run_id,
                base_commit=ctx.worktree.base_commit,
                expect=self._owned(),
            )
            if not taken:
                raise EngineError(f"{bead.id}: run {run_id} was cancelled before it could dispatch")
            log.info(
                "%s: run %s started (%s) in %s",
                bead.id,
                run_id,
                recipe_name,
                ctx.worktree.path,
            )
        else:
            self._refuse_if_superseded(bead, run_id, expect)
            self.store.reconcile_inflight()
            repair: dict[str, Any] = {}
            if prior is not None and not prior.get("worktree"):
                # Adopting a row whose fresh pass never got far enough to
                # fill these in (a crash between promoting to `running`
                # and that first update) -- backfill them now rather than
                # leaving them NULL forever; nothing else ever will.
                repair = dict(
                    worktree=str(ctx.worktree.path),
                    branch=ctx.worktree.branch,
                    log_dir=str(ctx.log_dir),
                    base_commit=ctx.worktree.base_commit,
                )
            if not self.store.update_run(
                run_id,
                bead_id=bead.id,
                outbox=dispatch_outbox,
                status=RUN_RUNNING,
                pid=os.getpid(),
                expect=expect,
                **repair,
            ):
                raise EngineError(f"{bead.id}: run {run_id} was taken over or cancelled before it could resume")
            self.store.mark_resumed(run_id)
            log.info("%s: run %s resumed (%s)", bead.id, run_id, recipe_name)

    def _settle(
        self,
        ctx: RunContext,
        bead: Bead,
        recipe_name: str,
        run_id: str,
        final: dict[str, Any],
    ) -> RunResult:
        pending = final.get("__interrupt__")
        if pending:
            payload = _interrupt_payload(pending)
            retry_at = payload.get("retry_at") or None
            scheduled = f"; the scheduler resumes it after {retry_at}" if retry_at else ""
            if not self.store.update_run(
                run_id,
                bead_id=bead.id,
                expect=self._owned(),
                outbox=[
                    ("status", {"status": bd.STATUS_WAITING_HUMAN, "if_status": bd.STATUS_IMPLEMENTING}),
                    ("metadata", {"values": {bd.META_STAGE: "waiting-human"}}),
                    (
                        "note",
                        {
                            "text": f"alloy: waiting for human -- {payload.get('reason', '')} "
                            f"(resume with `alloy resume {bead.id}`{scheduled})"
                        },
                    ),
                ],
                status=RUN_WAITING_HUMAN,
                stage="waiting-human",
                pid=None,
                retry_at=retry_at,
                event_reason=str(payload.get("reason", "")),
            ):
                raise self._lost_settle(bead.id, run_id, "waiting-human")
            self.store.mark_paused(run_id)
            deliver_pending(self.store, self.beads, bead_id=bead.id)
            return RunResult(
                bead.id,
                run_id,
                Outcome.WAITING_HUMAN.value,
                reason=str(payload.get("reason", "")),
                interrupt=payload,
                worktree=str(ctx.worktree.path),
            )

        outcome = final.get("outcome") or Outcome.FAILED.value
        reason = final.get("outcome_reason", "")

        if outcome == Outcome.DONE.value:
            # The commit happens *before* any of this is recorded, local or
            # bd-facing: if the *process* dies here the run stays `running`,
            # recoverable the ordinary way. But if `commit_wip` merely
            # *raises* while the owning process survives (e.g. the live
            # scheduler, running this in-process) -- nothing would ever mark
            # the row terminal: its pid stays genuinely alive, so
            # `orphaned_runs()` never adopts it, and a single bad commit would
            # permanently occupy the scheduler's (usually one) concurrency
            # slot. So a raised commit_wip is caught here specifically, just
            # to settle the run as failed, then re-raised unchanged.
            try:
                commit_sha = ctx.worktrees.commit_wip(ctx.worktree, f"{bead.id}: {bead.title}")
            except Exception as exc:
                log.exception("%s: run %s verified done but commit_wip failed", bead.id, run_id)
                settled = self.store.finish_run(
                    run_id,
                    status=RUN_FAILED,
                    outcome=Outcome.FAILED.value,
                    reason=f"commit failed after a done verdict: {exc}",
                    bead_id=bead.id,
                    outbox=[
                        ("status", {"status": bd.STATUS_FAILED, "if_status": bd.STATUS_IMPLEMENTING}),
                        (
                            "note",
                            {
                                "text": f"alloy: {recipe_name} verified done, but committing the work failed: "
                                f"{exc}. Worktree kept at {ctx.worktree.path}"
                            },
                        ),
                    ],
                    expect=self._owned(),
                )
                if settled:
                    deliver_pending(self.store, self.beads, bead_id=bead.id)
                else:
                    self._log_lost_settle(bead.id, run_id, "failed")
                raise
            # `committed_sha` (even the "none" sentinel, for a verification-
            # only task with nothing to commit) is what lets reconcile tell
            # "done and verified" from "done locally, commit unconfirmed" --
            # see `reconcile._status_after`.
            landed = final.get("budget_landed") or None
            settled = self.store.finish_run(
                run_id,
                expect=self._owned(),
                status=RUN_DONE,
                outcome=outcome,
                reason=reason,
                bead_id=bead.id,
                committed_sha=commit_sha or "none",
                outbox=[
                    ("status", {"status": bd.STATUS_DONE, "if_status": bd.STATUS_IMPLEMENTING}),
                    ("metadata", {"values": {bd.META_STAGE: "finished"}}),
                    (
                        "note",
                        {
                            "text": f"alloy: {recipe_name} succeeded in {final.get('iteration', 0)} iteration(s) "
                            f"on branch {ctx.worktree.branch}. {reason}"
                        },
                    ),
                    *(_budget_landed_outbox(landed) if landed else []),
                ],
            )
            if settled and landed:
                log.warning(
                    "%s: run %s budget-landed (%s); marked %s for audit",
                    bead.id,
                    run_id,
                    landed.get("limit"),
                    bd.LABEL_BUDGET_LANDED,
                )
                self.store.events.emit(
                    EVENT_BUDGET_LANDED,
                    bead=bead.id,
                    run=run_id,
                    reason=str(landed.get("limit", "")),
                    regression=landed.get("regression"),
                    green_checks=landed.get("green_checks"),
                )
        else:
            settled = self.store.finish_run(
                run_id,
                expect=self._owned(),
                status=RUN_FAILED,
                outcome=outcome,
                reason=reason,
                bead_id=bead.id,
                outbox=[
                    ("status", {"status": bd.STATUS_FAILED, "if_status": bd.STATUS_IMPLEMENTING}),
                    ("metadata", {"values": {bd.META_STAGE: outcome}}),
                    (
                        "note",
                        {
                            "text": f"alloy: {recipe_name} failed after {final.get('iteration', 0)} iteration(s): "
                            f"{reason}. Worktree kept at {ctx.worktree.path}"
                        },
                    ),
                ],
            )
        if not settled:
            raise self._lost_settle(bead.id, run_id, outcome)
        deliver_pending(self.store, self.beads, bead_id=bead.id)
        return RunResult(bead.id, run_id, outcome, reason=reason, worktree=str(ctx.worktree.path))

    def _log_lost_settle(self, bead_id: str, run_id: str, outcome: str) -> None:
        current = self.store.get_run(run_id) or {}
        log.warning(
            "%s: run %s finished %s, but its row was already moved to %s (pid %s) by someone else; "
            "not recording it, and not telling bd",
            bead_id,
            run_id,
            outcome,
            current.get("status"),
            current.get("pid"),
        )

    def _lost_settle(self, bead_id: str, run_id: str, outcome: str) -> EngineError:
        self._log_lost_settle(bead_id, run_id, outcome)
        return EngineError(
            f"{bead_id}: run {run_id} finished {outcome} after it had already been cancelled or taken over"
        )

    def _resumable_run(self, bead_id: str) -> dict[str, Any] | None:
        """A run left behind by a crash: marked running, but nobody is running it."""
        record = self.store.latest_run_for_bead(bead_id)
        if record is None:
            return None
        if record["status"] == RUN_RUNNING and not _pid_alive(record["pid"]):
            return record
        return None

    def graph_snapshot(self, bead_id: str) -> dict[str, Any] | None:
        """The last persisted graph state of this bead's most recent run."""
        record = self.store.latest_run_for_bead(bead_id)
        if record is None:
            return None
        return read_checkpoint(self.paths.workflows_db, record["thread_id"])

    def graph_snapshot_for_run(self, run_id: str) -> dict[str, Any] | None:
        """The last persisted graph state for this specific run."""
        record = self.store.get_run(run_id)
        if record is None:
            return None
        return read_checkpoint(self.paths.workflows_db, record["thread_id"])


def _budget_landed_outbox(landed: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """bd side effects that make a budget-landed bead findable for an audit
    (`bd list --label budget-landed`)."""
    green = ", ".join(f"`{command}`" for command in landed.get("green_checks") or []) or "(none recorded)"
    return [
        ("metadata", {"values": {bd.META_BUDGET_LANDED: "true"}}),
        ("label", {"label": bd.LABEL_BUDGET_LANDED}),
        (
            "note",
            {
                "text": f"alloy: budget-landed -- the run hit {landed.get('limit')} while the acceptance gate "
                f"only wanted more verification; it was landed as done because every check was green on an "
                f"unchanged tree. Regression check: `{landed.get('regression')}`. Green checks: {green}. "
                "Audit with `bd list --label budget-landed`."
            },
        ),
    ]


def _interrupt_payload(pending: Any) -> dict[str, Any]:
    if isinstance(pending, (list, tuple)) and pending:
        first = pending[0]
        value = getattr(first, "value", first)
        return value if isinstance(value, dict) else {"reason": str(value)}
    if isinstance(pending, dict):
        return pending
    return {"reason": str(pending)}


def _pid_alive(pid: int | None) -> bool:
    return pid_alive(pid)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
