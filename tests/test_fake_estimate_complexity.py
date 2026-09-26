"""Fake estimate harness output must validate as ComplexityEstimate.

Scheduler and engine tests script agents without an explicit ``estimate`` entry
in ``ALLOY_FAKE_CONFIG``. The estimate role still runs (jev defers to the
cursor fallback under fake harness), but the fake CLI emits no structured
output today. ``classify`` then falls back to medium with a bead note like
``estimate failed: ... input_value=None``.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from alloy.beads import META_COMPLEXITY_ESTIMATED
from alloy.config import load_recipe
from alloy.engine import Engine
from alloy.models import COMPLEXITY_LEVELS, ComplexityEstimate
from alloy.recipes.tdd_loop import ESTIMATE_STATIC
from alloy.runners.base import extract_json_object
from alloy.runners.claude import ClaudeRunner
from alloy.runners.cursor import CursorRunner
from alloy.scheduler import Scheduler
from conftest import bd_create
from support import make_harness
from test_scheduler import _bead_notes, script as scheduler_script
from test_workflow import script as workflow_script


def _run_fake_cli(fake_harnesses, binary: str, prompt: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(fake_harnesses.bindir / binary), "-p", prompt],
        capture_output=True,
        text=True,
        check=True,
        env=os.environ.copy(),
    )


def _structured_from_runner_output(
    runner,
    proc: subprocess.CompletedProcess[str],
) -> dict | None:
    text, structured, _usage, _session_id, failed = runner.parse(proc.stdout, proc.stderr, proc.returncode)
    assert not failed
    if structured is None and text:
        structured = extract_json_object(text)
    return structured


@pytest.mark.parametrize(
    ("runner_cls", "binary"),
    [
        (ClaudeRunner, "claude"),
        (CursorRunner, "cursor-agent"),
    ],
)
def test_fake_cli_estimate_emits_valid_complexity_estimate_without_config_entry(
    fake_harnesses,
    runner_cls,
    binary: str,
):
    """Empty ALLOY_FAKE_CONFIG must still yield ComplexityEstimate-shaped output."""
    fake_harnesses.configure({})
    proc = _run_fake_cli(fake_harnesses, binary, ESTIMATE_STATIC)
    structured = _structured_from_runner_output(runner_cls(), proc)
    assert structured is not None
    estimate = ComplexityEstimate.model_validate(structured)
    assert estimate.complexity in COMPLEXITY_LEVELS


def _tdd_loop_run(engine: Engine, bead_id: str) -> dict:
    """The bead's tdd-loop run: a successful tick also auto-lands the bead
    (tdd-loop ships landing.mode auto), so the latest run is the land run."""
    return next(
        record
        for record in engine.store.all_runs(limit=1000)
        if record["bead_id"] == bead_id and record["recipe"] == "tdd-loop"
    )


def _estimate_calls(engine: Engine, bead_id: str) -> list[dict]:
    run = _tdd_loop_run(engine, bead_id)
    return [c for c in engine.store.agent_calls(run["run_id"]) if c["role"] == "estimate"]


def _validated_estimate_from_calls(calls: list[dict]) -> ComplexityEstimate:
    for call in calls:
        raw = call.get("structured_json")
        if not raw:
            continue
        return ComplexityEstimate.model_validate(json.loads(raw))
    raise AssertionError("no estimate call recorded valid structured_json")


async def test_scheduler_tick_without_estimate_script_records_valid_estimate(
    beads_project,
    alloy_home,
    fake_harnesses,
):
    """Scheduler tests omit an estimate script entry; estimate must still classify."""
    fake_harnesses.configure(scheduler_script())
    bead_id = bd_create(beads_project, "first", priority=0, alloy_recipe="tdd-loop")
    scheduler = Scheduler(
        engine=Engine.open(beads_project, alloy_home),
        poll_seconds=0.01,
        once=True,
    )

    assert await scheduler.tick() is True

    engine = scheduler.engine
    bead = engine.beads.show(bead_id)
    notes = _bead_notes(engine, bead_id)
    run = _tdd_loop_run(engine, bead_id)
    estimate_calls = _estimate_calls(engine, bead_id)

    assert estimate_calls, "estimate role should run during a scheduler tick"
    assert "estimate failed" not in notes
    assert "(default)" not in notes
    assert "(estimate)" in notes

    estimate = _validated_estimate_from_calls(estimate_calls)
    assert run["complexity"] == estimate.complexity
    assert bead.metadata.get(META_COMPLEXITY_ESTIMATED) == estimate.complexity


async def test_engine_run_with_builtin_recipe_without_estimate_script_records_valid_estimate(
    beads_project,
    alloy_home,
    fake_harnesses,
):
    """Engine runs the builtin recipe (jev estimate + cursor fallback), not support.load_config."""
    fake_harnesses.configure(workflow_script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    engine = Engine.open(beads_project, alloy_home)
    harness = make_harness(
        beads_project,
        alloy_home,
        bead=engine.beads.show(bead_id),
        config=load_recipe("tdd-loop"),
        beads=engine.beads,
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    bead = engine.beads.show(bead_id)
    notes = _bead_notes(engine, bead_id)
    estimate_calls = [c for c in harness.store.agent_calls(harness.run_id) if c["role"] == "estimate"]

    assert final["complexity_source"] == "estimate"
    assert "estimate failed" not in notes
    assert "(default)" not in notes

    estimate = _validated_estimate_from_calls(estimate_calls)
    assert final["complexity"] == estimate.complexity
    assert bead.metadata.get(META_COMPLEXITY_ESTIMATED) == estimate.complexity
