"""LangGraph checkpoint storage.

One SQLite file under ~/.alloy holds every workflow's graph state. The nodes are
async (they shell out to harnesses), so execution uses the async saver; the
read-only inspection path used by `alloy status` opens the same file
synchronously.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


@asynccontextmanager
async def open_checkpointer(path: Path) -> AsyncIterator[AsyncSqliteSaver]:
    """A checkpointer bound to `path`, set up and closed around the caller."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(str(path), timeout=30.0)
    try:
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA busy_timeout=30000")
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
        yield saver
    finally:
        # Drain queued writes outside the cancelled task. If cancellation arrives
        # during close, finish cleanup before letting the caller resume the run.
        closing = asyncio.create_task(connection.close())
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await closing
            raise


def read_checkpoint(path: Path, thread_id: str) -> dict[str, Any] | None:
    """Last persisted state for a thread, without running anything."""
    if not Path(path).exists():
        return None
    connection = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
    try:
        saver = SqliteSaver(connection)
        saver.setup()
        snapshot = saver.get_tuple({"configurable": {"thread_id": thread_id}})
        if snapshot is None:
            return None
        return {
            "values": snapshot.checkpoint.get("channel_values", {}),
            "checkpoint_id": snapshot.config.get("configurable", {}).get("checkpoint_id"),
            "interrupts": [
                write for write in (snapshot.pending_writes or [])
                if write[1] == "__interrupt__"
            ],
            "metadata": dict(snapshot.metadata or {}),
        }
    finally:
        connection.close()


def has_pending_interrupt(path: Path, thread_id: str) -> bool:
    snapshot = read_checkpoint(path, thread_id)
    return bool(snapshot and snapshot["interrupts"])
