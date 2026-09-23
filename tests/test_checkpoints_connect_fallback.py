"""Checkpoint open path when ``aiosqlite.connect`` stalls (alloy-qw7).

Reproduces the sandbox/asyncio self-pipe denial that hung bare
``aiosqlite.connect(':memory:')`` and engine-backed runs for ~35s.
``open_checkpointer`` must detect the stall and fall back to threaded sync
sqlite on the same file, pragmas and schema.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import aiosqlite
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from alloy.checkpoints import open_checkpointer, read_checkpoint
from alloy.engine import Engine
from conftest import (
    bd_create,
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    write_tests_entry,
)
from support import await_cancelled_task, await_role, make_harness

# Reported stall was ~35s; fallback must open well inside the cancel budget.
CONNECT_OPEN_BUDGET_S = 6.0
CANCEL_AWAIT_BUDGET_S = 6.0


def script(**overrides):
    base = {
        "context": context_entry(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "judge": [judge_entry("done")],
        "critic": critic_entry(),
        "synthesize": synthesize_entry(),
    }
    base.update(overrides)
    return base


async def _open_checkpointer_within_budget(db_path, budget_s: float) -> float:
    started = time.monotonic()
    async with asyncio.timeout(budget_s):
        async with open_checkpointer(db_path) as _checkpointer:
            pass
    return time.monotonic() - started


def _pragma_values(db_path) -> tuple[str, int]:
    connection = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30.0)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        return journal_mode, busy_timeout
    finally:
        connection.close()


async def test_open_checkpointer_completes_when_aiosqlite_connect_stalls(
    tmp_path,
    simulate_stalled_aiosqlite_connect,
):
    """open_checkpointer must not hang on a stalled aiosqlite.connect."""
    db_path = tmp_path / "workflows.db"
    elapsed = await _open_checkpointer_within_budget(db_path, CONNECT_OPEN_BUDGET_S)
    assert elapsed < CONNECT_OPEN_BUDGET_S


async def test_open_checkpointer_slow_connect_does_not_trigger_fallback(
    tmp_path,
    monkeypatch,
):
    """A slow (1-1.5s) but healthy connect must stay on the aiosqlite path."""
    real_connect = aiosqlite.connect

    def slow_connect(*args, **kwargs):
        async def delayed() -> aiosqlite.Connection:
            await asyncio.sleep(1.2)
            return await real_connect(*args, **kwargs)

        return delayed()

    monkeypatch.setattr(aiosqlite, "connect", slow_connect)
    db_path = tmp_path / "workflows.db"
    async with asyncio.timeout(CONNECT_OPEN_BUDGET_S):
        async with open_checkpointer(db_path) as saver:
            assert isinstance(saver, AsyncSqliteSaver)


async def test_open_checkpointer_sets_wal_and_busy_timeout_when_connect_stalls(
    tmp_path,
    simulate_stalled_aiosqlite_connect,
):
    """Fallback must keep the same WAL and busy_timeout pragmas as the async path."""
    db_path = tmp_path / "workflows.db"
    await _open_checkpointer_within_budget(db_path, CONNECT_OPEN_BUDGET_S)

    journal_mode, busy_timeout = _pragma_values(db_path)
    assert journal_mode == "wal"
    assert busy_timeout == 30000


async def test_open_checkpointer_async_checkpoint_roundtrip_when_connect_stalls(
    tmp_path,
    simulate_stalled_aiosqlite_connect,
):
    """Async saver operations and sync read_checkpoint must share the same file."""
    db_path = tmp_path / "workflows.db"
    thread_id = "connect-fallback-thread"

    async with asyncio.timeout(CONNECT_OPEN_BUDGET_S):
        async with open_checkpointer(db_path) as saver:
            config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
            checkpoint = {
                "v": 1,
                "ts": "connect-fallback",
                "id": "cf-1",
                "channel_values": {"stage": "baseline"},
                "channel_versions": {},
                "versions_seen": {},
            }
            await saver.aput(config, checkpoint, {"source": "connect-fallback"}, {})

    snapshot = read_checkpoint(db_path, thread_id)
    assert snapshot is not None
    assert snapshot["values"]["stage"] == "baseline"
    assert snapshot["checkpoint_id"] == "cf-1"


async def test_engine_run_completes_when_aiosqlite_connect_stalls(
    beads_project,
    alloy_home,
    fake_harnesses,
    simulate_stalled_aiosqlite_connect,
):
    """Engine._execute must finish when the async SQLite connect path is unusable."""
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)

    result = await asyncio.wait_for(engine.run(bead_id), timeout=30.0)
    assert result.outcome == "done"


async def test_harness_start_cancel_finishes_when_aiosqlite_connect_stalls(
    project,
    alloy_home,
    fake_harnesses,
    simulate_stalled_aiosqlite_connect,
    simulate_slow_sqlite_close_under_cancel,
):
    """Connect fallback must still bound checkpointer teardown under cancellation."""
    harness = make_harness(project, alloy_home)
    fake_harnesses.configure(script(implement=[{"sleep": 30}]))
    task = asyncio.create_task(harness.start())
    await await_role(fake_harnesses, "implement", timeout=30)
    task.cancel()
    elapsed = await await_cancelled_task(task, budget_s=CANCEL_AWAIT_BUDGET_S)
    assert elapsed < CANCEL_AWAIT_BUDGET_S
