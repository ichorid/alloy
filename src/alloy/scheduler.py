"""The scheduler: poll Beads, claim one task, run it, repeat.

Deliberately dumb. It contains no LLM and no heuristics -- readiness and priority
are Beads' answers, and concurrency is one. Everything interesting happens inside
the workflow it starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from alloy import beads as bd
from alloy.engine import Engine, EngineError
from alloy.models import utcnow
from alloy.store import RUN_RUNNING, RUN_WAITING_HUMAN

DEFAULT_POLL_SECONDS = 15.0

log = logging.getLogger("alloy.scheduler")


class SchedulerBusy(RuntimeError):
    pass


@dataclass
class Scheduler:
    engine: Engine
    poll_seconds: float = DEFAULT_POLL_SECONDS
    concurrency: int = 1
    recipe_filter: str | None = None
    once: bool = False
    _stopping: bool = field(default=False, init=False)
    _cancel_requested: bool = field(default=False, init=False)
    _current: "asyncio.Task | None" = field(default=None, init=False)

    # -- lifecycle --------------------------------------------------------

    async def serve(self) -> None:
        self._write_pidfile()
        self._install_signal_handlers()
        log.info("scheduler up (pid %d, poll %.0fs, repo %s)",
                 os.getpid(), self.poll_seconds, self.engine.repo)
        try:
            await self.recover()
            while not self._stopping:
                started = await self.tick()
                if self.once:
                    return
                if not started:
                    await self._sleep(self.poll_seconds)
        finally:
            self._remove_pidfile()
            log.info("scheduler down")

    async def recover(self) -> list[str]:
        """Adopt runs whose process died -- the reboot-survival path."""
        self.engine.store.reconcile_inflight()
        recovered: list[str] = []
        for record in self.engine.store.orphaned_runs():
            bead_id = record["bead_id"]
            log.info("recovering %s (run %s left by a dead process)", bead_id, record["run_id"])
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
        due = self.due_resume()
        if due is not None:
            return await self._resume_due(due)
        bead = self.next_task()
        if bead is None:
            return False
        log.info("picked %s (%s, P%d)", bead.id, bead.title, bead.priority)
        try:
            await self._run_current(bead.id)
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
        return True

    async def _resume_due(self, record: dict) -> bool:
        """journal 38: the harness said when it would be back; that time has passed."""
        bead_id = record["bead_id"]
        log.info("resuming %s (run %s, retry_at %s passed)",
                 bead_id, record["run_id"], record.get("retry_at"))
        try:
            await self._run_current(bead_id, resume=True)
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

    async def _run_current(self, bead_id: str, *, resume: bool = False):
        """Run one bead as a task we can cancel from a signal handler."""
        coro = self.engine.resume(bead_id, "") if resume else self.engine.run(bead_id)
        self._current = asyncio.ensure_future(coro)
        try:
            return await self._current
        finally:
            self._current = None

    # -- selection --------------------------------------------------------

    def next_task(self) -> bd.Bead | None:
        """Highest-priority ready bead that carries a recipe Alloy knows."""
        from alloy import recipes

        known = set(recipes.names())
        for bead in self.engine.beads.ready(recipe=self.recipe_filter):
            if bead.recipe in known:
                return bead
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
    repo: Path, root: Path | None, poll_seconds: float, *, log_file: Path | None = None
) -> int:
    """Start `alloy start --foreground` as a background process.

    Output goes to `log_file` (appended) so a scheduler that dies overnight
    leaves a trace, not silence.
    """
    argv = [
        sys.executable, "-m", "alloy.cli", "start", "--foreground",
        "--repo", str(repo), "--poll", str(poll_seconds),
    ]
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
