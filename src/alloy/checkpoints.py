"""LangGraph checkpoint storage.

One SQLite file under ~/.alloy holds every workflow's graph state. The nodes are
async (they shell out to harnesses), so execution uses the async saver; the
read-only inspection path used by `alloy status` opens the same file
synchronously.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

log = logging.getLogger(__name__)

# Sandboxes that deny asyncio self-pipe writes hang aiosqlite.connect forever;
# a healthy connect takes milliseconds, so treat anything past this as stalled.
CONNECT_STALL_TIMEOUT_S = 2.0


class _ThreadedSqliteSaver(SqliteSaver):
    """SqliteSaver with the async checkpoint interface run in worker threads.

    Fallback for environments where aiosqlite.connect cannot complete: the
    graph still awaits the usual async methods, but each one delegates to the
    sync implementation via asyncio.to_thread on a check_same_thread=False
    connection.
    """

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        tuples = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for checkpoint_tuple in tuples:
            yield checkpoint_tuple

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(
            self.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)


@asynccontextmanager
async def _open_threaded_checkpointer(path: Path) -> AsyncIterator[_ThreadedSqliteSaver]:
    """Sync-sqlite fallback keeping the same file, pragmas and schema."""
    connection = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        saver = _ThreadedSqliteSaver(connection)
        await asyncio.to_thread(saver.setup)
        yield saver
    finally:
        await asyncio.to_thread(connection.close)


@asynccontextmanager
async def open_checkpointer(path: Path) -> AsyncIterator[AsyncSqliteSaver | SqliteSaver]:
    """A checkpointer bound to `path`, set up and closed around the caller."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = await asyncio.wait_for(
            aiosqlite.connect(str(path), timeout=30.0),
            timeout=CONNECT_STALL_TIMEOUT_S,
        )
    except TimeoutError:
        log.warning(
            "aiosqlite.connect stalled past %.1fs; using threaded sqlite fallback",
            CONNECT_STALL_TIMEOUT_S,
        )
        async with _open_threaded_checkpointer(Path(path)) as saver:
            yield saver
        return
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
