"""Starting, resuming and finishing one task's workflow.

The engine is the only place that knows how Beads status, the worktree, the run
ledger and the LangGraph checkpoint fit together -- and it keeps them consistent
whether a run ends normally, pauses for a human, or dies mid-flight.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from langgraph.types import Command

from alloy import beads as bd
from alloy import recipes
from alloy.beads import Bead, BeadsClient
from alloy.checkpoints import has_pending_interrupt, open_checkpointer, read_checkpoint
from alloy.config import ConfigError, RecipeConfig, load_recipe
from alloy.models import Outcome
from alloy.paths import AlloyPaths
from alloy.procs import pid_alive, terminate_group, terminate_pid
from alloy.runners import RunnerRegistry
from alloy.runtime import RunContext
from alloy.store import (
    RUN_CANCELLED,
    RUN_DONE,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_WAITING_HUMAN,
    Store,
)
from alloy.worktree import Worktree, WorktreeError, WorktreeManager, is_test_path

RECURSION_LIMIT = 200

# Decides whether a remediation child's diff may be merged into its parent:
# (bug bead, diff against base) -> (ok, reason).
MergeGate = Callable[[Bead, str], Awaitable[tuple[bool, str]]]

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
    merge_gate: MergeGate | None = None

    @classmethod
    def open(cls, repo: Path | str, root: Path | str | None = None) -> "Engine":
        paths = AlloyPaths.resolve(root).ensure()
        repo = Path(repo).resolve()
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
        self._refuse_if_live(bead_id, latest)
        existing = self._resumable_run(bead_id)
        if existing is not None:
            log.info("%s: adopting orphaned run %s", bead_id, existing["run_id"])
            # Nothing ran between the owner's last write and now; that dead
            # time must not eat the wall-time budget (see Store.mark_resumed).
            if not existing.get("paused_at"):
                self.store.update_run(existing["run_id"], paused_at=existing["updated_at"])
            return await self._execute(
                bead, existing["recipe"], run_id=existing["run_id"],
                thread_id=existing["thread_id"], resume_payload=None,
            )

        name = recipe_name or bead.recipe
        if not name:
            raise EngineError(
                f"{bead_id} has no recipe; set one with "
                f"`bd update {bead_id} --set-metadata {bd.META_RECIPE}=tdd-loop`"
            )
        # Everything that can be checked without touching the bead is checked
        # first: a claim followed by a crash used to strand the bead in
        # `implementing` with no run record for recovery to find.
        self.validate_recipe(name)
        if bead.status not in (bd.STATUS_READY, bd.STATUS_IMPLEMENTING):
            raise EngineError(
                f"{bead_id} is '{bead.status}'; only '{bd.STATUS_READY}' beads can start"
            )
        claimed = False
        if bead.status == bd.STATUS_READY:
            if not self.beads.claim(bead_id):
                raise EngineError(f"{bead_id} was claimed by someone else")
            claimed = True
        try:
            return await self._execute(bead, name, run_id=None, resume_payload=None)
        except EngineError:
            # Failed before a run record existed (worktree, config): hand the
            # bead back so it is offered again instead of looking busy forever.
            if claimed and self.store.latest_run_for_bead(bead_id) == latest:
                self.beads.set_status(bead_id, bd.STATUS_READY, if_status=bd.STATUS_IMPLEMENTING)
            raise

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
        return await self._execute(
            bead,
            record["recipe"],
            run_id=record["run_id"],
            thread_id=record["thread_id"],
            resume_payload={"instructions": instructions} if pending else None,
        )

    def cancel(self, bead_id: str, *, grace_s: float = 5.0) -> bool:
        """Stop the run -- the process too, not just the bookkeeping."""
        record = self.store.latest_run_for_bead(bead_id)
        if record is None or record["status"] in (RUN_DONE, RUN_FAILED, RUN_CANCELLED):
            return False
        pid = record.get("pid")
        if record["status"] == RUN_RUNNING and pid_alive(pid) and pid != os.getpid():
            log.info("%s: stopping pid %s", bead_id, pid)
            if not terminate_pid(int(pid), grace_s=grace_s):
                raise EngineError(f"could not stop pid {pid} running {bead_id}")
        # journal 9: a run that died without cleanup (SIGKILL, OOM) leaves its
        # harness process group running; the inflight row remembers its pid.
        for call in self.store.active_calls(record["run_id"]):
            harness_pid = call.get("pid")
            if harness_pid and pid_alive(harness_pid):
                log.info("%s: stopping orphaned harness pid %s", bead_id, harness_pid)
                terminate_group(int(harness_pid), grace_s=grace_s)
            self.store.discard_call(call["call_id"])
        self.store.finish_run(
            record["run_id"], status=RUN_CANCELLED,
            outcome=Outcome.CANCELLED.value, reason="cancelled by operator",
        )
        self.beads.set_status(bead_id, bd.STATUS_READY)
        self.beads.note(bead_id, f"alloy: run {record['run_id']} cancelled; "
                                 f"worktree left at {record['worktree']}")
        return True

    def validate_recipe(self, name: str) -> RecipeConfig:
        """Both halves of a recipe must exist: the YAML and the graph builder."""
        try:
            recipes.get(name)
            return self.load_config(name)
        except (KeyError, ConfigError) as exc:
            raise EngineError(str(exc)) from exc

    # -- remediation ------------------------------------------------------

    async def run_child(
        self, bead_id: str, *, parent: RunContext, merge_gate: MergeGate | None = None
    ) -> RunResult:
        """Fix a bug bead from inside a parent run and merge the fix into it.

        The parent's uncommitted work is parked in a WIP commit; the child is
        cut from the parent's *base* (the defect predates the parent's work, so
        it reproduces there against a green suite); when the child is done its
        diff passes a deterministic guard and the merge gate before being
        merged into the parent's worktree. Any failure leaves the parent tree
        clean at the WIP commit.
        """
        parent_record = self.store.get_run(parent.run_id)
        if parent_record is None:
            raise EngineError(f"no run recorded for parent {parent.run_id}")
        if parent_record.get("parent_run_id"):
            raise EngineError(
                f"run {parent.run_id} is itself a remediation child of "
                f"{parent_record['parent_run_id']}; remediation is one level deep"
            )
        bug = self.beads.show(bead_id)
        recipe_name = bug.recipe or parent.recipe.name
        self.validate_recipe(recipe_name)
        if bug.status not in (bd.STATUS_READY, bd.STATUS_IMPLEMENTING):
            raise EngineError(
                f"{bead_id} is '{bug.status}'; only '{bd.STATUS_READY}' beads can start"
            )
        if bug.status == bd.STATUS_READY and not self.beads.claim(bead_id):
            raise EngineError(f"{bead_id} was claimed by someone else")

        worktrees = parent.worktrees
        # Read before the WIP commit: the parent's base is where its work
        # started, and the child must never see what came after.
        base = parent.worktree.base_commit
        wip_sha = worktrees.commit_wip(
            parent.worktree, f"alloy: wip before remediating {bead_id}"
        )
        try:
            child_wt = worktrees.ensure_from(bead_id, base)
        except WorktreeError as exc:
            raise EngineError(str(exc)) from exc

        result = await self._execute(
            bug, recipe_name, run_id=None, resume_payload=None,
            thread_id=f"{parent.run_id}/{bead_id}", worktree=child_wt,
            parent_run_id=parent.run_id,
        )
        if result.outcome != Outcome.DONE.value:
            return result

        worktrees.commit_wip(child_wt, f"alloy: fix {bead_id} (remediating {parent.bead.id})")

        touched = self._guarded_test_paths(worktrees, child_wt, base, wip_sha)
        if touched:
            return self._fail_child(
                bug, result, parent,
                "test-file guard: the fix modifies test file(s) added by the parent's "
                f"work in progress: {', '.join(touched)}",
            )

        diff = worktrees.diff(child_wt)
        if merge_gate is None:
            return self._fail_child(bug, result, parent, "merge gate failed: no merge gate bound")
        ok, reason = await merge_gate(bug, diff)
        if not ok:
            return self._fail_child(bug, result, parent, f"merge gate rejected the fix: {reason}")

        merged = worktrees.merge_branch(parent.worktree, child_wt.branch)
        if not merged.ok:
            return self._fail_child(
                bug, result, parent,
                f"merge conflict merging {child_wt.branch} into {parent.worktree.branch}: "
                f"{', '.join(merged.conflict_files) or 'unknown files'}",
            )

        note = (f"alloy: remediation of {bead_id} merged into {parent.worktree.branch} "
                f"for {parent.bead.id} (parent run {parent.run_id})")
        self.beads.note(bead_id, note)
        self.beads.note(parent.bead.id, note)
        log.info("%s: child %s merged into %s", parent.bead.id, bead_id, parent.worktree.branch)
        return result

    def _guarded_test_paths(
        self, worktrees: WorktreeManager, child_wt: Worktree, base: str, wip_sha: str | None
    ) -> list[str]:
        """Test files the parent's WIP commit added that the child changed.

        A fix that rewrites or removes the task's own tests can never be
        right. Adding the same test with identical content is not a change.
        """
        if wip_sha is None:
            return []
        wip_tests = [p for p in worktrees.added_paths(wip_sha) if is_test_path(p)]
        child_changed = set(worktrees.changed_paths(child_wt, base))
        touched = [p for p in wip_tests if p in child_changed]
        if not touched:
            return []
        return worktrees.changed_paths(child_wt, wip_sha, until="HEAD", paths=touched)

    def _fail_child(
        self, bug: Bead, result: RunResult, parent: RunContext, reason: str
    ) -> RunResult:
        """The child finished but its fix cannot land: record why on both sides.
        The parent tree is already clean at its WIP commit; the child branch
        stays for a human to inspect."""
        self.store.finish_run(result.run_id, status=RUN_FAILED,
                              outcome=Outcome.FAILED.value, reason=reason)
        self.beads.set_status(bug.id, bd.STATUS_FAILED)
        self.beads.set_metadata(bug.id, {bd.META_STAGE: Outcome.FAILED.value})
        self.beads.note(bug.id, f"alloy: remediation for {parent.bead.id} not merged -- {reason}. "
                                f"Branch kept at {result.worktree}")
        self.beads.note(parent.bead.id,
                        f"alloy: remediation of {bug.id} not merged -- {reason}")
        log.warning("%s: child %s not merged: %s", parent.bead.id, bug.id, reason)
        return RunResult(bug.id, result.run_id, Outcome.FAILED.value, reason=reason,
                         worktree=result.worktree)

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
        return load_recipe(name, alloy_root=self.paths.root, project=self.repo)

    def build_context(
        self, bead: Bead, recipe_name: str, *, run_id: str, checkpointer: Any,
        worktree: Worktree | None = None,
    ) -> RunContext:
        config = self.load_config(recipe_name)
        worktrees = WorktreeManager(repo=self.repo, root=self.paths.worktrees)
        if worktree is None:
            worktree = worktrees.ensure(bead.id)
        log_dir = self.paths.run_logs(run_id)
        log_dir.mkdir(parents=True, exist_ok=True)
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
        )
        merge_gate = self.merge_gate
        if merge_gate is None:
            # Default gate: the recipe's scope role judges the fix against the
            # project context packet; anything but `merge` keeps it out.
            from alloy.recipes.tdd_loop import scope_merge_gate

            async def merge_gate(bug: Bead, diff: str) -> tuple[bool, str]:
                return await scope_merge_gate(ctx, bug, diff)

        ctx.remediator = lambda bug_id: self.run_child(bug_id, parent=ctx, merge_gate=merge_gate)
        return ctx

    # -- internals --------------------------------------------------------

    async def _execute(
        self,
        bead: Bead,
        recipe_name: str,
        *,
        run_id: str | None,
        resume_payload: dict[str, Any] | None,
        thread_id: str | None = None,
        worktree: Worktree | None = None,
        parent_run_id: str | None = None,
    ) -> RunResult:
        recipe = recipes.get(recipe_name)
        fresh = run_id is None
        run_id = run_id or uuid.uuid4().hex
        # One graph thread per run, not per bead: re-running a cancelled bead
        # must start from an empty graph, not inherit the abandoned one.
        thread_id = thread_id or run_id

        async with open_checkpointer(self.paths.workflows_db) as checkpointer:
            try:
                ctx = self.build_context(
                    bead, recipe_name, run_id=run_id, checkpointer=checkpointer,
                    worktree=worktree,
                )
            except (ConfigError, KeyError, WorktreeError) as exc:
                raise EngineError(str(exc)) from exc

            if fresh:
                self.store.create_run(
                    run_id=run_id, bead_id=bead.id, thread_id=thread_id, recipe=recipe_name,
                    repo=self.repo, worktree=ctx.worktree.path, branch=ctx.worktree.branch,
                    log_dir=ctx.log_dir, parent_run_id=parent_run_id,
                )
                log.info("%s: run %s started (%s) in %s",
                         bead.id, run_id, recipe_name, ctx.worktree.path)
            else:
                self.store.reconcile_inflight()
                self.store.mark_resumed(run_id)
                self.store.update_run(run_id, status=RUN_RUNNING, pid=os.getpid())
                log.info("%s: run %s resumed (%s)", bead.id, run_id, recipe_name)

            self.beads.set_metadata(bead.id, {
                bd.META_RUN_ID: run_id,
                bd.META_WORKTREE: str(ctx.worktree.path),
                bd.META_BRANCH: ctx.worktree.branch,
                bd.META_RECIPE: recipe_name,
            })
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
                self.store.finish_run(run_id, status=RUN_FAILED,
                                      outcome=Outcome.FAILED.value, reason=str(exc))
                self.beads.set_status(bead.id, bd.STATUS_FAILED)
                self.beads.note(bead.id, f"alloy: run {run_id} crashed: {exc}. "
                                         f"Worktree kept at {ctx.worktree.path}")
                raise
            except BaseException:
                log.warning("%s: run %s interrupted; it stays resumable", bead.id, run_id)
                raise

            result = self._settle(ctx, bead, recipe_name, run_id, final)
            log.info("%s: run %s -> %s %s", bead.id, run_id, result.outcome, result.reason)
            return result

    def _settle(
        self, ctx: RunContext, bead: Bead, recipe_name: str, run_id: str, final: dict[str, Any]
    ) -> RunResult:
        config = ctx.recipe
        pending = final.get("__interrupt__")
        if pending:
            payload = _interrupt_payload(pending)
            self.store.update_run(run_id, status=RUN_WAITING_HUMAN, stage="waiting-human",
                                  pid=None)
            self.store.mark_paused(run_id)
            self.beads.set_status(bead.id, bd.STATUS_WAITING_HUMAN)
            self.beads.set_metadata(bead.id, {bd.META_STAGE: "waiting-human"})
            self.beads.note(
                bead.id,
                f"alloy: waiting for human -- {payload.get('reason', '')} "
                f"(resume with `alloy resume {bead.id}`)",
            )
            return RunResult(bead.id, run_id, Outcome.WAITING_HUMAN.value,
                             reason=str(payload.get("reason", "")), interrupt=payload,
                             worktree=str(ctx.worktree.path))

        outcome = final.get("outcome") or Outcome.FAILED.value
        reason = final.get("outcome_reason", "")

        if outcome == Outcome.DONE.value:
            self.store.finish_run(run_id, status=RUN_DONE, outcome=outcome, reason=reason)
            self.beads.set_status(bead.id, config.on_success_status)
            self.beads.set_metadata(bead.id, {bd.META_STAGE: "finished"})
            self.beads.note(
                bead.id,
                f"alloy: {recipe_name} succeeded in {final.get('iteration', 0)} iteration(s) "
                f"on branch {ctx.worktree.branch}. {reason}",
            )
            if config.cleanup_worktree_on_success:
                ctx.worktrees.remove(bead.id)
        else:
            self.store.finish_run(run_id, status=RUN_FAILED, outcome=outcome, reason=reason)
            self.beads.set_status(bead.id, bd.STATUS_FAILED)
            self.beads.set_metadata(bead.id, {bd.META_STAGE: outcome})
            self.beads.note(
                bead.id,
                f"alloy: {recipe_name} failed after {final.get('iteration', 0)} iteration(s): "
                f"{reason}. Worktree kept at {ctx.worktree.path}",
            )
        return RunResult(bead.id, run_id, outcome, reason=reason,
                         worktree=str(ctx.worktree.path))

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

