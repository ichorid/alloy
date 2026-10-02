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
from alloy.models import Outcome
from alloy.paths import AlloyPaths
from alloy.procs import pid_alive, read_pid, terminate_group, terminate_pid
from alloy.runners import RunnerRegistry
from alloy.runtime import RunContext
from alloy.store import (
    RUN_CANCELLED,
    RUN_CLAIMING,
    RUN_DONE,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_WAITING_HUMAN,
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
        bead = self.beads.show(bead_id)
        latest = self.store.latest_run_for_bead(bead_id)
        if latest is not None and latest["status"] == RUN_CLAIMING and not pid_alive(latest.get("pid")):
            # A crash between creating the row and confirming the bd claim.
            # There is always something to resolve here -- unlike claiming bd
            # first, which could strand the bead `implementing` with no row
            # anywhere for recovery to find.
            latest = self._resolve_stuck_claiming(latest)
            bead = self.beads.show(bead_id)
        self._refuse_if_live(bead_id, latest)
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

        run_id = uuid.uuid4().hex
        claimed = False
        if bead.status == bd.STATUS_READY:
            # The row is created before bd is ever touched: a crash between
            # this and the claim leaves a `claiming` row with nothing in bd
            # yet (discarded below on a lost race, or by `recover()` on a
            # crash); a crash between the claim and promoting to `running`
            # leaves a row bd already agrees the bead is claimed for (also
            # resolved by `recover()`). Either window always has a row.
            self.store.create_claiming_run(
                run_id=run_id,
                bead_id=bead_id,
                thread_id=run_id,
                recipe=name,
                repo=self.repo,
                worktree=None,
                branch=None,
                log_dir=None,
            )
            if not self.beads.claim(bead_id):
                self.store.discard_claiming_run(run_id)
                raise EngineError(f"{bead_id} was claimed by someone else")
            claimed = True
            self.store.update_run(run_id, status=RUN_RUNNING)
        else:
            # Already `implementing` with no row (a stuck-claiming retry, or
            # pre-dating this mechanism): no new bd claim needed.
            self.store.create_run(
                run_id=run_id,
                bead_id=bead_id,
                thread_id=run_id,
                recipe=name,
                repo=self.repo,
                worktree=None,
                branch=None,
                log_dir=None,
            )
        try:
            return await self._execute(bead, name, run_id=run_id, thread_id=run_id, resume_payload=None, fresh=True)
        except EngineError as exc:
            # Failed before any real work started (worktree, config): hand the
            # bead back so it is offered again instead of looking busy forever.
            if claimed:
                current = self.store.get_run(run_id)
                if current is not None and current["status"] == RUN_RUNNING:
                    self.store.finish_run(run_id, status=RUN_CANCELLED, outcome=Outcome.CANCELLED.value, reason=str(exc))
                    self.beads.set_status(bead_id, bd.STATUS_READY, if_status=bd.STATUS_IMPLEMENTING)
            raise

    def _resolve_stuck_claiming(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """A `claiming` row survived a crash with its owning process gone.

        If bd never saw the claim, there is nothing to adopt: discard the row
        and let the bead be offered fresh. If bd shows it `implementing`, the
        claim did land -- promote the row to `running` (filling in the
        worktree/branch/log_dir a crash this early never recorded) so the
        ordinary orphan-adoption path in `run()`/`Scheduler.recover()` picks
        it up exactly like any other crash survivor.
        """
        bead_id = record["bead_id"]
        run_id = record["run_id"]
        if self._bead_status(bead_id) != bd.STATUS_IMPLEMENTING:
            self.store.discard_claiming_run(run_id)
            return None
        worktrees = WorktreeManager(repo=self.repo, root=self.paths.worktrees)
        self.store.update_run(
            run_id,
            status=RUN_RUNNING,
            worktree=str(self.repo),
            branch=worktrees.current_branch() or "HEAD",
            log_dir=str(self.paths.run_logs(run_id)),
        )
        return self.store.get_run(run_id)

    async def resume(self, bead_id: str, instructions: str = "") -> RunResult:
        """Continue a paused or crashed run, optionally answering the human gate."""
        record = self.store.latest_run_for_bead(bead_id)
        if record is None:
            raise EngineError(f"no run recorded for {bead_id}")
        if record["status"] in (RUN_DONE, RUN_CANCELLED):
            raise EngineError(f"run for {bead_id} already finished ({record['status']})")
        self._refuse_if_live(bead_id, record)
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
        record = self.store.latest_run_for_bead(bead_id)
        if record is None or record["status"] in (RUN_DONE, RUN_FAILED, RUN_CANCELLED):
            return False
        pid = record.get("pid")
        if record["status"] == RUN_RUNNING and pid_alive(pid) and pid != os.getpid():
            scheduler_pid = read_pid(self.paths.scheduler_pid)
            if scheduler_pid is not None and int(pid) == scheduler_pid:
                return self._cancel_via_scheduler(bead_id, record, scheduler_pid, timeout_s=scheduler_timeout_s)
            log.info("%s: stopping pid %s", bead_id, pid)
            if not terminate_pid(int(pid), grace_s=grace_s):
                raise EngineError(f"could not stop pid {pid} running {bead_id}")
        self._cancel_bookkeeping(bead_id, record, grace_s=grace_s)
        return True

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

    def _cancel_bookkeeping(self, bead_id: str, record: dict[str, Any], *, grace_s: float) -> None:
        """Book a stopped run cancelled: its orphaned harnesses, its still-active
        remediation children, and the bead (back to ready unless a human
        closed it meanwhile -- a cancel must never reopen finished work)."""
        for child in self.store.children_of(record["run_id"]):
            if child["status"] in (RUN_RUNNING, RUN_WAITING_HUMAN):
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
        self.store.finish_run(
            record["run_id"],
            status=RUN_CANCELLED,
            outcome=Outcome.CANCELLED.value,
            reason="cancelled by operator",
        )
        if self._bead_status(bead_id) == bd.STATUS_DONE:
            self.beads.note(bead_id, f"alloy: run {record['run_id']} cancelled; the bead stays closed")
            return
        self.beads.set_status(bead_id, bd.STATUS_READY)
        self.beads.note(
            bead_id,
            f"alloy: run {record['run_id']} cancelled; worktree left at {record['worktree']}",
        )

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
        if (
            record is not None
            and record["status"] == RUN_RUNNING
            and pid_alive(record.get("pid"))
            and record.get("pid") != os.getpid()
        ):
            raise EngineError(
                f"{bead_id} is already being run by pid {record['pid']} "
                f"(run {record['run_id']}); `alloy cancel {bead_id}` stops it"
            )

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
        )
        return ctx

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
    ) -> RunResult:
        """`fresh=True` means `run_id` already names a `running` row `run()`
        created and populated with bd's claim before calling here (see
        `Engine.run`); this only fills in the worktree/branch/log_dir/
        base_commit a claim-time row cannot know yet. `fresh=False` (resuming
        an existing run, orphaned or paused) never touches those -- they were
        already recorded by this same run's own earlier fresh pass."""
        recipe = recipes.get(recipe_name)
        # One graph thread per run, not per bead: re-running a cancelled bead
        # must start from an empty graph, not inherit the abandoned one.
        thread_id = thread_id or run_id

        async with open_checkpointer(self.paths.workflows_db) as checkpointer:
            recorded_base = None
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

            if fresh:
                # The row already exists (`run()` created it, running, before
                # ever calling here) -- this only fills in what a claim-time
                # row cannot know yet.
                self.store.update_run(
                    run_id,
                    worktree=str(ctx.worktree.path),
                    branch=ctx.worktree.branch,
                    log_dir=str(ctx.log_dir),
                    parent_run_id=parent_run_id,
                    base_commit=ctx.worktree.base_commit,
                )
                log.info(
                    "%s: run %s started (%s) in %s",
                    bead.id,
                    run_id,
                    recipe_name,
                    ctx.worktree.path,
                )
            else:
                self.store.reconcile_inflight()
                self.store.mark_resumed(run_id)
                self.store.update_run(run_id, status=RUN_RUNNING, pid=os.getpid())
                log.info("%s: run %s resumed (%s)", bead.id, run_id, recipe_name)

            self.beads.set_metadata(
                bead.id,
                {
                    bd.META_RUN_ID: run_id,
                    bd.META_WORKTREE: str(ctx.worktree.path),
                    bd.META_BRANCH: ctx.worktree.branch,
                    bd.META_RECIPE: recipe_name,
                },
            )
            if bead.status != bd.STATUS_IMPLEMENTING:
                self.beads.set_status(bead.id, bd.STATUS_IMPLEMENTING)

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
                final = await graph.ainvoke(payload, config)
            except Exception as exc:
                # A cancellation is not an Exception, so an interrupted process
                # leaves the run marked running -- which is what makes it
                # recoverable later. Only a genuine error fails the task here.
                log.exception("%s: run %s crashed", bead.id, run_id)
                self.store.finish_run(
                    run_id,
                    status=RUN_FAILED,
                    outcome=Outcome.FAILED.value,
                    reason=str(exc),
                )
                self.beads.set_status(bead.id, bd.STATUS_FAILED)
                self.beads.note(
                    bead.id,
                    f"alloy: run {run_id} crashed: {exc}. Worktree kept at {ctx.worktree.path}",
                )
                raise
            except BaseException:
                log.warning("%s: run %s interrupted; it stays resumable", bead.id, run_id)
                raise

            result = self._settle(ctx, bead, recipe_name, run_id, final)
            log.info("%s: run %s -> %s %s", bead.id, run_id, result.outcome, result.reason)
            return result

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
            self.store.update_run(
                run_id,
                status=RUN_WAITING_HUMAN,
                stage="waiting-human",
                pid=None,
                retry_at=retry_at,
                event_reason=str(payload.get("reason", "")),
            )
            self.store.mark_paused(run_id)
            self.beads.set_status(bead.id, bd.STATUS_WAITING_HUMAN)
            self.beads.set_metadata(bead.id, {bd.META_STAGE: "waiting-human"})
            scheduled = f"; the scheduler resumes it after {retry_at}" if retry_at else ""
            self.beads.note(
                bead.id,
                f"alloy: waiting for human -- {payload.get('reason', '')} "
                f"(resume with `alloy resume {bead.id}`{scheduled})",
            )
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
            self.store.finish_run(run_id, status=RUN_DONE, outcome=outcome, reason=reason)
            ctx.worktrees.commit_wip(ctx.worktree, f"{bead.id}: {bead.title}")
            self.beads.set_status(bead.id, bd.STATUS_DONE)
            self.beads.set_metadata(bead.id, {bd.META_STAGE: "finished"})
            self.beads.note(
                bead.id,
                f"alloy: {recipe_name} succeeded in {final.get('iteration', 0)} iteration(s) "
                f"on branch {ctx.worktree.branch}. {reason}",
            )
        else:
            self.store.finish_run(run_id, status=RUN_FAILED, outcome=outcome, reason=reason)
            self.beads.set_status(bead.id, bd.STATUS_FAILED)
            self.beads.set_metadata(bead.id, {bd.META_STAGE: outcome})
            self.beads.note(
                bead.id,
                f"alloy: {recipe_name} failed after {final.get('iteration', 0)} iteration(s): "
                f"{reason}. Worktree kept at {ctx.worktree.path}",
            )
        return RunResult(bead.id, run_id, outcome, reason=reason, worktree=str(ctx.worktree.path))

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
