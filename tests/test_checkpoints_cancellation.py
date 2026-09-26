"""Checkpoint teardown under in-process graph cancellation.

Production ``Engine.cancel()`` kills the owning OS process, so
``open_checkpointer``'s ``finally: await connection.close()`` has never been
exercised on a live, cancelled ``graph.ainvoke()`` in-process. Tests that
interrupt a harness or engine run with ``create_task`` + ``task.cancel()`` +
``await task`` must finish promptly and leave a resumable checkpoint.

The ``simulate_slow_sqlite_close_under_cancel`` fixture reproduces the
``PRAGMA busy_timeout``-scale stall triage observed (~75s): without a bounded
close in ``checkpoints.py``, ``await task`` after ``task.cancel()`` exceeds the
acceptance budget.
"""

from __future__ import annotations

import asyncio

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

from alloy.checkpoints import open_checkpointer, read_checkpoint
from alloy.engine import Engine

# Acceptance: cancel + await must not stall in checkpointer teardown (reported ~75s).
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


async def _cancel_harness_at_implement(harness, fake_harnesses) -> float:
    """Cancel ``harness.start()`` once implement is logged; return elapsed seconds."""
    fake_harnesses.configure(script(implement=[{"sleep": 30}]))
    task = asyncio.create_task(harness.start())
    await await_role(fake_harnesses, "implement", timeout=30)
    task.cancel()
    return await await_cancelled_task(task, budget_s=CANCEL_AWAIT_BUDGET_S)


async def test_harness_start_cancel_finishes_within_checkpointer_budget(
    project,
    alloy_home,
    fake_harnesses,
    simulate_slow_sqlite_close_under_cancel,
):
    """Cancelling harness.start() must not hang in open_checkpointer finally."""
    harness = make_harness(project, alloy_home)
    elapsed = await _cancel_harness_at_implement(harness, fake_harnesses)
    assert elapsed < CANCEL_AWAIT_BUDGET_S


async def test_engine_run_cancel_finishes_within_checkpointer_budget(
    beads_project,
    alloy_home,
    fake_harnesses,
    simulate_slow_sqlite_close_under_cancel,
):
    """Engine._execute uses the same checkpointer path as Harness.start()."""
    fake_harnesses.configure(script(implement=[{"sleep": 30}]))
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)

    task = asyncio.create_task(engine.run(bead_id))
    await await_role(fake_harnesses, "implement", timeout=30)
    task.cancel()
    elapsed = await await_cancelled_task(task, budget_s=CANCEL_AWAIT_BUDGET_S)
    assert elapsed < CANCEL_AWAIT_BUDGET_S


async def test_open_checkpointer_teardown_is_bounded_under_cancel(
    tmp_path,
    simulate_slow_sqlite_close_under_cancel,
):
    """Minimal repro: cancelled work inside open_checkpointer must exit promptly."""
    db_path = tmp_path / "workflows.db"

    async def work() -> None:
        async with open_checkpointer(db_path) as _checkpointer:
            await asyncio.sleep(30)

    task = asyncio.create_task(work())
    await asyncio.sleep(0.05)
    task.cancel()
    elapsed = await await_cancelled_task(task, budget_s=CANCEL_AWAIT_BUDGET_S)
    assert elapsed < CANCEL_AWAIT_BUDGET_S


async def test_cancelled_harness_start_leaves_resumable_checkpoint(
    project,
    alloy_home,
    fake_harnesses,
    simulate_slow_sqlite_close_under_cancel,
):
    """After a bounded cancel, the checkpoint on disk is intact and resume works."""
    harness = make_harness(project, alloy_home)
    await _cancel_harness_at_implement(harness, fake_harnesses)

    assert [call["role"] for call in fake_harnesses.calls] == [
        "context",
        "estimate",
        "tests",
        "implement",
    ]

    snapshot = read_checkpoint(alloy_home / "workflows.db", harness.thread_id)
    assert snapshot is not None
    assert snapshot["values"]["stage"] == "baseline"

    fake_harnesses.reset_calls()
    fake_harnesses.configure(script())

    final = await harness.resume(None)

    assert final["outcome"] == "done"
    assert [call["role"] for call in fake_harnesses.calls] == [
        "implement",
        "verifier",
        "acceptance",
        "judge",
        "harvest",
    ]
