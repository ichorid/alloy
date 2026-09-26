"""Shared CLI service and output helpers."""

from __future__ import annotations

import asyncio
import json as jsonlib
import signal
from pathlib import Path
from typing import Any, Coroutine, Optional, TypeVar

import typer
from rich.console import Console

from alloy.engine import Engine


def _engine(repo: Optional[Path], root: Optional[Path]) -> Engine:
    return Engine.open(repo or Path.cwd(), root)


def _run_async(coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run` with SIGINT/SIGTERM turned into a cancellation.

    Cancellation unwinds through the graph into the runner, which kills the
    harness's process group, and leaves the run marked running with a dead
    pid -- i.e. resumable. The default SIGTERM disposition would have killed
    only this process and left the harness editing the worktree.
    """

    async def main() -> T:
        task = asyncio.ensure_future(coro)
        loop = asyncio.get_running_loop()
        installed = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, task.cancel)
                installed.append(sig)
            except (NotImplementedError, ValueError):
                pass
        try:
            return await task
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)

    return asyncio.run(main())


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        console.print_json(jsonlib.dumps(payload, default=str))


def _fail(message: str) -> None:
    err.print(f"[red]error:[/red] {message}")
    raise typer.Exit(1)


console = Console()

err = Console(stderr=True)

T = TypeVar("T")
