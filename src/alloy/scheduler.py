"""The scheduler: poll Beads, claim one task, run it, repeat.

Deliberately dumb. It contains no LLM and no heuristics -- readiness and priority
are Beads' answers, and concurrency is one. Everything interesting happens inside
the workflow it starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from alloy import beads as bd
from alloy.config import ConfigError, MemorySpec
from alloy.engine import Engine, EngineError, RunResult
from alloy.events import EVENT_STALLED
from alloy.memory_schedule import (
    MEMORY_REVIEW_RECIPE,
    apply_review,
    dirty_instruction_files,
    embed_instruction_files,
    review_bead_for_note,
    review_due,
    review_plan,
    review_run_id,
)
from alloy.models import (
    DEFAULT_RECIPE_KEY,
    EMBED_STALE_KEY,
    Outcome,
    ProjectMemory,
    utcnow,
)
from alloy.paths import AlloyPaths
from alloy.store import RUN_DONE, RUN_FAILED, RUN_RUNNING, RUN_WAITING_HUMAN, _pid_alive
from alloy.worktree import WorktreeManager

DEFAULT_POLL_SECONDS = 15.0
DEFAULT_STALL_MINUTES = 30.0
HUMAN_RESUME_MEMORY_PREFIX = "alloy:human:"

log = logging.getLogger("alloy.scheduler")


class SchedulerBusy(RuntimeError):
    pass


@dataclass
class Scheduler:
    engine: Engine
    poll_seconds: float = DEFAULT_POLL_SECONDS
    concurrency: int = 1
    recipe_filter: str | None = None
    """When set, forces this recipe as the session default for unassigned
    beads (see next_task), overriding the alloy:default:recipe memory key.
    Validated eagerly in __post_init__: an unknown recipe raises EngineError
    rather than being silently skipped, unlike a bad memory default."""
    once: bool = False
    clock: Callable[[], datetime] = utcnow
    stall_minutes: float = DEFAULT_STALL_MINUTES
    _stopping: bool = field(default=False, init=False)
    _cancel_requested: bool = field(default=False, init=False)
    _current: "asyncio.Task | None" = field(default=None, init=False)
    _default_recipe: str | None = field(default=None, init=False)
    _unknown_default_recipe: str | None = field(default=None, init=False)
    _epic_block_logged: set[str] = field(default_factory=set, init=False)
    _stalled: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        if self.recipe_filter is not None:
            self.engine.validate_recipe(self.recipe_filter)

    # -- lifecycle --------------------------------------------------------

    async def serve(self) -> None:
        self._write_pidfile()
        self._write_session()
        self._install_signal_handlers()
        log.info(
            "scheduler up (pid %d, poll %.0fs, repo %s)",
            os.getpid(),
            self.poll_seconds,
            self.engine.repo,
        )
        watcher = None if self.once else asyncio.create_task(self._stall_watch())
        try:
            await self.recover()
            while not self._stopping:
                started = await self.tick()
                if self.once:
                    return
                if not started:
                    await self._sleep(self.poll_seconds)
        finally:
            if watcher is not None:
                watcher.cancel()
            self._finish_session()
            self._remove_pidfile()
            log.info("scheduler down")

    async def _stall_watch(self) -> None:
        """Runs beside `tick`, which blocks for the whole of a run."""
        while True:
            try:
                self.check_stalls()
            except Exception:
                log.exception("stall check failed")
            await asyncio.sleep(self.poll_seconds)

    def check_stalls(self) -> list[str]:
        """Announce each running run that has shown no sign of life for
        `stall_minutes` (or whose process died). One event per stall: the run
        is remembered until it moves again or ends, then may stall anew."""
        if self.stall_minutes <= 0:
            return []
        store = self.engine.store
        now = self.clock()
        limit = timedelta(minutes=self.stall_minutes)
        running = [r for r in store.active_runs(self.engine.repo) if r["status"] == RUN_RUNNING]
        flagged: list[str] = []
        current: set[str] = set()
        for run in running:
            idle = now - store.last_activity(run)
            dead = not _pid_alive(run["pid"])
            if idle < limit and not dead:
                continue
            current.add(run["run_id"])
            if run["run_id"] in self._stalled:
                continue
            calls = store.active_calls(run["run_id"])
            what = f"agent call {calls[0]['role']} still running" if calls else f"stage {run.get('stage') or '?'}"
            why = "run process is gone" if dead else f"no activity for {int(idle.total_seconds() // 60)} min ({what})"
            log.warning("%s: run %s stalled: %s", run["bead_id"], run["run_id"], why)
            store.events.emit(
                EVENT_STALLED,
                bead=run["bead_id"],
                run=run["run_id"],
                reason=why,
                stage=run.get("stage"),
            )
            flagged.append(run["run_id"])
        self._stalled = current
        return flagged

    async def recover(self) -> list[str]:
        """Adopt runs whose process died -- the reboot-survival path."""
        self.engine.store.reconcile_inflight()
        recovered: list[str] = []
        for record in self.engine.store.orphaned_runs():
            bead_id = record["bead_id"]
            log.info(
                "recovering %s (run %s left by a dead process)",
                bead_id,
                record["run_id"],
            )
            try:
                await self._run_current(bead_id)
                recovered.append(bead_id)
            except asyncio.CancelledError:
                if self._cancel_requested:
                    return recovered
                raise
            except EngineError as exc:
                log.warning("could not recover %s: %s", bead_id, exc)
            except Exception:
                log.exception("recovery of %s crashed", bead_id)
        return recovered

    async def tick(self) -> bool:
        """Claim and run at most one ready task. True if work was started."""
        if len(self._running()) >= self.concurrency:
            return False
        due = self.due_resume() or self.due_human_resume()
        if due is not None:
            return await self._resume_due(due)
        if await self._memory_maintenance():
            return True
        if self._auto_land_enabled() and await self._land_ready_work():
            return True
        with self._beads_snapshot():
            bead = self.next_task()
        if bead is None:
            return False
        log.info("picked %s (%s, P%d)", bead.id, bead.title, bead.priority)
        recipe_name = bead.recipe or self._default_recipe
        try:
            result = await self._run_current(bead.id, recipe_name=recipe_name)
        except asyncio.CancelledError:
            if self._cancel_requested:
                return False  # the operator stopped this run; serve() exits next
            raise
        except EngineError as exc:
            log.warning("%s refused: %s", bead.id, exc)
            return False
        except Exception:
            log.exception("%s failed", bead.id)
            return True
        await self._auto_land_after_run(bead, recipe_name, result)
        return True

    async def _auto_land_after_run(self, bead, recipe_name: str, result) -> None:
        if self._auto_land_enabled() and self._auto_land_due(bead, recipe_name, result):
            if not self.engine._commit_before_land(bead):
                log.warning("%s: auto-land skipped; worktree still dirty", bead.id)
            else:
                try:
                    await self.engine.land(bead.id)
                except asyncio.CancelledError:
                    raise
                except EngineError as exc:
                    log.warning("%s did not land: %s", bead.id, exc)
                except Exception:
                    log.exception("%s landing crashed", bead.id)

    async def _resume_due(self, record: dict) -> bool:
        """journal 38: the harness said when it would be back; that time has passed."""
        bead_id = record["bead_id"]
        retry_at = record.get("retry_at")
        if retry_at:
            log.info(
                "resuming %s (run %s, retry_at %s passed)",
                bead_id,
                record["run_id"],
                retry_at,
            )
        else:
            log.info(
                "resuming %s (run %s, human gate, bead ready)",
                bead_id,
                record["run_id"],
            )
        instructions = self._human_resume_instructions(bead_id) if retry_at is None else ""
        try:
            await self._run_current(bead_id, resume=True, resume_instructions=instructions)
        except asyncio.CancelledError:
            if self._cancel_requested:
                return False
            raise
        except EngineError as exc:
            log.warning("%s could not be resumed: %s", bead_id, exc)
            self.engine.store.update_run(record["run_id"], retry_at=None)
            return False
        except Exception:
            log.exception("%s failed after auto-resume", bead_id)
        return True

    async def _run_current(
        self,
        bead_id: str,
        *,
        resume: bool = False,
        recipe_name: str | None = None,
        resume_instructions: str = "",
    ):
        """Run one bead as a task we can cancel from a signal handler."""
        coro = (
            self.engine.resume(bead_id, resume_instructions)
            if resume
            else self.engine.run(bead_id, recipe_name=recipe_name)
        )
        self._current = asyncio.ensure_future(coro)
        try:
            return await self._current
        finally:
            self._current = None

    # -- memory maintenance (alloy-4ef.19) ----------------------------------

    async def _memory_maintenance(self) -> bool:
        """Run `memory review --apply` then embed when due: at most once per
        calendar day and never while a run is active. True if it ran."""
        if self._running():
            return False
        store = self.engine.store
        today = self.clock().date()
        last_ran_day = _parse_day(store.last_memory_review_day())
        if last_ran_day == today:
            return False  # once per calendar day; skip the bd read entirely
        try:
            config = self.engine.load_config(MEMORY_REVIEW_RECIPE)
            if not config.memory.enabled:
                return False
            memory = ProjectMemory.from_raw(self.engine.beads.memories(), config.memory)
        except (ConfigError, bd.BeadsError) as exc:
            log.debug("memory maintenance skipped: %s", exc)
            return False
        reason = review_due(
            memory,
            config.memory,
            today=today,
            finished_runs=store.finished_runs_since_last_review(),
            last_ran_day=last_ran_day,
        )
        if reason is None:
            return False
        log.info("memory review due: %s", reason)
        store.set_last_memory_review_day(today.isoformat())
        run_id = review_run_id()
        try:
            plan = await review_plan(self.engine, MEMORY_REVIEW_RECIPE, memory, run_id, today=today)
            applied = apply_review(self.engine, plan, run_id, today=today)
            store.set_finished_runs_since_last_review(0)
            if EMBED_STALE_KEY in memory.entries:
                self.engine.beads.forget(EMBED_STALE_KEY)
            self._embed_after_review(config.memory, applied)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("memory review %s failed", run_id)
            return False
        return True

    def _embed_after_review(self, spec: MemorySpec, applied: dict) -> None:
        dirty = dirty_instruction_files(self.engine.repo, spec.instruction_files)
        if dirty:
            files = ", ".join(dirty)
            log.warning("memory embed skipped: %s has uncommitted changes", files)
            self.engine.beads.note(
                review_bead_for_note(self.engine, applied),
                f"alloy: memory embed skipped -- {files} has uncommitted changes; "
                "commit or revert them, then run `alloy memory embed`",
            )
            return
        memory = ProjectMemory.from_raw(self.engine.beads.memories(), spec)
        changed = embed_instruction_files(self.engine.repo, memory, spec)
        log.info("memory embed updated %s", ", ".join(changed) or "nothing")

    # -- selection --------------------------------------------------------

    def _beads_snapshot(self):
        """One `bd list --all` behind a poll-time scan (no-op for clients without it)."""
        snapshot = getattr(self.engine.beads, "snapshot", None)
        return snapshot() if snapshot is not None else contextlib.nullcontext()

    def next_task(self) -> bd.Bead | None:
        """Highest-priority ready bead with a known assigned or default recipe."""
        from alloy import recipes

        known = set(recipes.names())
        if self.recipe_filter is not None:
            # Validated in __post_init__; it stands in for the memory
            # default this session and wins over it, but still yields to a
            # bead's own alloy_recipe metadata (see the check below).
            self._default_recipe = self.recipe_filter
            self._unknown_default_recipe = None
        else:
            self._default_recipe = None
            default = self.engine.beads.memories().get(DEFAULT_RECIPE_KEY)
            if default and default not in known:
                if default != self._unknown_default_recipe:
                    log.warning(
                        "ignoring unknown default recipe %r from %s",
                        default,
                        DEFAULT_RECIPE_KEY,
                    )
                self._unknown_default_recipe = default
            else:
                self._unknown_default_recipe = None
                self._default_recipe = default or None
        for bead in self.engine.beads.ready(include_unassigned=self._default_recipe is not None):
            if (bead.recipe or self._default_recipe) not in known:
                continue
            if self._top_level_dispatch_blocked(bead.id):
                continue
            if self._epic_dispatch_blocked(bead.id):
                continue
            return bead
        return None

    def _top_level_unit(self, bead_id: str) -> str:
        """The epic root a bead belongs to, or the bead itself when standalone."""
        return self.engine.beads.epic_root(bead_id) or bead_id

    def _top_level_dispatch_blocked(self, bead_id: str) -> bool:
        """True while another epic or standalone bead has an unfinished run
        or sits at review-ready.

        Top-level units run strictly one after another: a run that is running
        or parked at waiting-human holds the repo until it lands, so a stuck
        bead never lets unrelated work pile up merge conflicts behind it.
        """
        unit = self._top_level_unit(bead_id)
        holders = [run["bead_id"] for run in self.engine.store.active_runs(self.engine.repo)]
        # A review-ready bead holds its unit until it lands, except for the
        # repair bug it is waiting on, which must run for it to ever land.
        for held in self.engine.beads.list_by_status(bd.STATUS_REVIEW_READY):
            if held.metadata.get(bd.META_LAND_REPAIR) != bead_id:
                holders.append(held.id)
        for holder_bead in holders:
            holder = self._top_level_unit(holder_bead)
            if holder == unit:
                continue
            if holder not in self._epic_block_logged:
                log.info("skipping %s: %s has unfinished work", unit, holder)
                self._epic_block_logged.add(holder)
            return True
        return False

    def _epic_dispatch_blocked(self, bead_id: str) -> bool:
        """True when an epic child must wait for a sibling or its own prior run."""
        epic_id = self.engine.beads.epic_root(bead_id)
        if epic_id is None:
            # An epic is not a child of itself: while any descendant is open the
            # children do the work and the epic lands when they are all closed.
            # Sorting puts the epic ahead of its children in `ready`, so without
            # this an idle epic with no run yet was picked for an empty
            # epic-level run.
            return bool(self.engine.beads.open_descendants(bead_id))
        blocker = self._epic_blocking_sibling(epic_id)
        if blocker is not None:
            if blocker not in self._epic_block_logged:
                log.info(
                    "skipping epic children: %s blocks dispatch",
                    blocker,
                )
                self._epic_block_logged.add(blocker)
            return True
        latest = self.engine.store.latest_run_for_bead(bead_id)
        return latest is not None and latest["status"] == RUN_DONE

    def _epic_blocking_sibling(self, epic_id: str) -> str | None:
        """Return a descendant id whose run holds the shared epic worktree."""
        for sibling in self.engine.beads.open_descendants(epic_id):
            run = self.engine.store.latest_run_for_bead(sibling.id)
            if run is None:
                continue
            if run["status"] in (RUN_RUNNING, RUN_WAITING_HUMAN):
                return sibling.id
            if run["status"] == RUN_FAILED and _worktree_dirty(run.get("worktree")):
                return sibling.id
        return None

    def due_resume(self) -> dict | None:
        """The oldest waiting-human run whose scheduled retry_at has passed."""
        now = utcnow()
        for run in self.engine.store.active_runs():
            if run["status"] != RUN_WAITING_HUMAN or not run.get("retry_at"):
                continue
            retry_at = _parse_iso(run["retry_at"])
            if retry_at is not None and retry_at <= now:
                return run
        return None

    def due_human_resume(self) -> dict | None:
        """A ready bead whose run is parked at the human gate without a retry_at."""
        parked = [
            run
            for run in self.engine.store.active_runs()
            if run["status"] == RUN_WAITING_HUMAN and not run.get("retry_at")
        ]
        if not parked:
            return None  # the common idle case: skip the bd read entirely
        ready_ids = {bead.id for bead in self.engine.beads.ready(limit=1000)}
        for run in parked:
            bead_id = run["bead_id"]
            if bead_id not in ready_ids:
                continue
            bead = self.engine.beads.show(bead_id)
            if not bead.recipe:
                continue
            return run
        return None

    def _human_resume_instructions(self, bead_id: str) -> str:
        return self.engine.beads.memories().get(f"{HUMAN_RESUME_MEMORY_PREFIX}{bead_id}", "")

    # -- auto-land (alloy-vrh.10, docs/plans/auto-land.md) ------------------

    def _auto_land_enabled(self) -> bool:
        """Local opt-out: touch ``<alloy-root>/disable-auto-land`` to skip auto-landing."""
        return not (self.engine.paths.root / "disable-auto-land").is_file()

    async def _land_ready_work(self) -> bool:
        """Land work whose time has come, before the next ready pick: completed
        epics and beads whose landing repair bug just closed. At most one
        landing per tick, matching the scheduler's concurrency of one; a
        landing that refuses (conflict, red checks, parked primary) is logged
        and left for a later tick or a human, never a serve-loop crash."""
        with self._beads_snapshot():
            due = self._completed_epics() + self._repaired_beads()
        for bead_id in due:
            bead = self.engine.beads.show(bead_id)
            if not self.engine._commit_before_land(bead):
                log.warning("landing %s skipped; worktree still dirty", bead_id)
                continue
            try:
                await self.engine.land(bead_id)
            except EngineError as exc:
                log.warning("landing %s did not complete: %s", bead_id, exc)
            except Exception:
                log.exception("landing %s crashed", bead_id)
            else:
                log.info("landed %s before picking new work", bead_id)
                return True
        return False

    def _completed_epics(self) -> list[str]:
        """Open epics whose descendants have all closed and that Alloy actually
        ran (manual or empty epics, never touched by a run, are left alone).

        An epic that opted into an isolated worktree (`alloy_use_worktree`) is
        marked by that worktree's presence; an in-place epic has no worktree,
        so a descendant carrying `alloy_run_id` is the signal instead."""
        worktrees = WorktreeManager(repo=self.engine.repo, root=self.engine.paths.worktrees)
        due: list[str] = []
        for bead in self.engine.beads.list_by_status(bd.STATUS_READY):
            if bead.issue_type != "epic":
                continue
            if self.engine.beads.open_descendants(bead.id):
                continue
            has_worktree = (worktrees.path_for(bead.id) / ".git").exists()
            if not has_worktree and not self._any_descendant_ran(bead.id):
                continue
            due.append(bead.id)
        return due

    def _any_descendant_ran(self, epic_id: str) -> bool:
        """Whether any descendant (closed or not) recorded an Alloy run -- the
        in-place equivalent of "this epic has a worktree"."""
        for child in self.engine.beads.children(epic_id):
            if child.issue_type == "epic":
                if self._any_descendant_ran(child.id):
                    return True
            elif child.metadata.get(bd.META_RUN_ID):
                return True
        return False

    def _repaired_beads(self) -> list[str]:
        """Review-ready beads in `repairing` whose repair bug is now closed."""
        due: list[str] = []
        for bead in self.engine.beads.list_by_status(bd.STATUS_REVIEW_READY):
            if bead.metadata.get(bd.META_LAND_STATE) != "repairing":
                continue
            bug_id = bead.metadata.get(bd.META_LAND_REPAIR)
            if not bug_id:
                continue
            try:
                bug = self.engine.beads.show(str(bug_id))
            except bd.BeadsError:
                continue
            if bug.status == bd.STATUS_DONE:
                due.append(bead.id)
        return due

    def _auto_land_due(self, bead: bd.Bead, recipe_name: str | None, result: RunResult) -> bool:
        """True when a just-settled standalone run's recipe opts into auto-landing."""
        if result.outcome != Outcome.DONE.value or not recipe_name:
            return False
        settled = self.engine.beads.show(bead.id)
        if settled.status != bd.STATUS_REVIEW_READY:
            return False  # epic children and repair bugs close on success instead
        try:
            config = self.engine.load_config(recipe_name)
        except (ConfigError, KeyError) as exc:
            log.warning("auto-land check for %s skipped: %s", bead.id, exc)
            return False
        return config.landing.mode == "auto"

    def _running(self) -> list[dict]:
        return [run for run in self.engine.store.active_runs() if run["status"] == RUN_RUNNING]

    # -- process control --------------------------------------------------

    def stop(self, *, now: bool = False) -> None:
        """First call: finish the current task, then exit. Second call (or
        `now`): cancel the current task -- its harness dies with it and the
        run stays resumable -- and exit."""
        if (self._stopping or now) and self._current is not None and not self._current.done():
            log.info("stop requested twice; cancelling the current run")
            self._cancel_requested = True
            self._current.cancel()
        elif not self._stopping:
            log.info("stop requested; finishing the current run first")
        self._stopping = True

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(seconds)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, self.stop)

    @property
    def pidfile(self) -> Path:
        return self.engine.paths.scheduler_pid

    def _write_pidfile(self) -> None:
        existing = read_pid(self.pidfile)
        if existing and existing != os.getpid():
            raise SchedulerBusy(f"scheduler already running (pid {existing})")
        self.pidfile.parent.mkdir(parents=True, exist_ok=True)
        self.pidfile.write_text(str(os.getpid()), encoding="utf-8")

    def _remove_pidfile(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            if read_pid(self.pidfile) == os.getpid():
                self.pidfile.unlink()

    def _write_session(self) -> None:
        path = self.engine.paths.scheduler_session
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "started_at": self.clock().isoformat(),
            "ended_at": None,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _finish_session(self) -> None:
        path = self.engine.paths.scheduler_session
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict) or data.get("pid") != os.getpid():
            return
        data["ended_at"] = self.clock().isoformat()
        path.write_text(json.dumps(data), encoding="utf-8")


def read_session(paths: AlloyPaths) -> dict | None:
    """Scheduler session metadata from scheduler.json, or None if absent/unreadable."""
    try:
        data = json.loads(paths.scheduler_session.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or set(data.keys()) != {
        "pid",
        "started_at",
        "ended_at",
    }:
        return None
    return data


def _worktree_dirty(worktree: str | None) -> bool:
    """True when the path has uncommitted git changes (porcelain output)."""
    if not worktree:
        return False
    path = Path(worktree)
    if not path.is_dir():
        return False
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    if proc.returncode != 0:
        return False
    return bool(proc.stdout.strip())


def _parse_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    # A naive time is local wall-clock time (the form "resets 1:20am" gives).
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def read_pid(pidfile: Path) -> int | None:
    """The pid of a live scheduler, or None (clearing a stale file)."""
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        with contextlib.suppress(FileNotFoundError):
            pidfile.unlink()
        return None
    except PermissionError:
        return pid
    return pid


def signal_stop(pidfile: Path, *, now: bool = False) -> int | None:
    """Ask the scheduler to stop; `now` also cancels the task it is running."""
    import time

    pid = read_pid(pidfile)
    if pid is None:
        return None
    os.kill(pid, signal.SIGTERM)
    if now:
        time.sleep(0.5)  # let the first handler run before the second signal
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    return pid


def spawn_detached(
    repo: Path,
    root: Path | None,
    poll_seconds: float,
    *,
    log_file: Path | None = None,
    stall_minutes: float = DEFAULT_STALL_MINUTES,
    recipe: str | None = None,
) -> int:
    """Start `alloy start --foreground` as a background process.

    Output goes to `log_file` (appended) so a scheduler that dies overnight
    leaves a trace, not silence.
    """
    argv = [
        sys.executable,
        "-m",
        "alloy.cli",
        "start",
        "--foreground",
        "--repo",
        str(repo),
        "--poll",
        str(poll_seconds),
        "--stall-minutes",
        str(stall_minutes),
    ]
    if recipe:
        argv += ["--recipe", recipe]
    if root:
        argv += ["--root", str(root)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        sink = open(log_file, "ab")
    else:
        sink = subprocess.DEVNULL
    try:
        process = subprocess.Popen(
            argv,
            stdout=sink,
            stderr=subprocess.STDOUT if log_file is not None else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        if log_file is not None:
            sink.close()
    return process.pid
