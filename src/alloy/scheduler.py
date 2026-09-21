"""The scheduler: poll Beads, claim one task, run it, repeat.

Deliberately dumb. It contains no LLM and no heuristics -- readiness and priority
are Beads' answers, and concurrency is one. Everything interesting happens inside
the workflow it starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from alloy import beads as bd
from alloy.engine import Engine, EngineError
from alloy.store import RUN_RUNNING

DEFAULT_POLL_SECONDS = 15.0


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

    # -- lifecycle --------------------------------------------------------

    async def serve(self) -> None:
        self._write_pidfile()
        self._install_signal_handlers()
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

    async def recover(self) -> list[str]:
        """Adopt runs whose process died -- the reboot-survival path."""
        recovered: list[str] = []
        for record in self.engine.store.orphaned_runs():
            bead_id = record["bead_id"]
            try:
                await self.engine.run(bead_id)
                recovered.append(bead_id)
            except EngineError:
                continue
            except Exception:
                continue
        return recovered

    async def tick(self) -> bool:
        """Claim and run at most one ready task. True if work was started."""
        if len(self._running()) >= self.concurrency:
            return False
        bead = self.next_task()
        if bead is None:
            return False
        try:
            await self.engine.run(bead.id)
        except EngineError:
            return False
        return True

    # -- selection --------------------------------------------------------

    def next_task(self) -> bd.Bead | None:
        """Highest-priority ready bead that carries a recipe Alloy knows."""
        from alloy import recipes

        known = set(recipes.names())
        for bead in self.engine.beads.ready(recipe=self.recipe_filter):
            if bead.recipe in known:
                return bead
        return None

    def _running(self) -> list[dict]:
        return [run for run in self.engine.store.active_runs() if run["status"] == RUN_RUNNING]

    # -- process control --------------------------------------------------

    def stop(self) -> None:
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


def signal_stop(pidfile: Path) -> int | None:
    pid = read_pid(pidfile)
    if pid is None:
        return None
    os.kill(pid, signal.SIGTERM)
    return pid


def spawn_detached(repo: Path, root: Path | None, poll_seconds: float) -> int:
    """Start `alloy start --foreground` as a background process."""
    argv = [
        sys.executable, "-m", "alloy.cli", "start", "--foreground",
        "--repo", str(repo), "--poll", str(poll_seconds),
    ]
    if root:
        argv += ["--root", str(root)]
    process = subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return process.pid
