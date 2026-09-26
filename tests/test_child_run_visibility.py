"""Acceptance tests for alloy-0uc.9: child runs in status and logs.

Behaviour is not implemented yet — these must fail until `alloy status --json`
surfaces parent_run_id, children, and remediating, and `alloy logs <parent>`
aggregates child agent calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alloy.checkpoints import open_checkpointer
from alloy.cli import app
from alloy.engine import Engine
from conftest import bd_create
from support import load_config
from test_engine_run_child import (
    _bug_script,
    _parent_context,
    _paused_parent_with_wip,
)

CHILD_RUNNER_NAMES = ("jev", "codex", "cursor-plan", "claude", "cursor-agent")


def _invoke(*args: str, project: Path, alloy_home: Path):
    runner = CliRunner()
    return runner.invoke(
        app,
        [*args, "--repo", str(project), "--root", str(alloy_home)],
    )


def _status_bead(payload: dict, bead_id: str) -> dict:
    for row in payload["beads"]:
        if row["bead"] == bead_id:
            return row
    raise AssertionError(f"bead {bead_id} not found in status payload")


@pytest.fixture
def engine(beads_project, alloy_home, monkeypatch):
    engine = Engine.open(beads_project, alloy_home)
    monkeypatch.setattr(engine, "load_config", lambda name: load_config())
    return engine


async def _parent_with_finished_child(
    engine: Engine,
    beads_project: Path,
    fake_harnesses,
) -> tuple[str, str, str, str]:
    """Return parent bead id, parent run id, child bead id, child run id."""
    parent_id, parent_run_id, _, _ = await _paused_parent_with_wip(
        engine,
        beads_project,
        fake_harnesses,
    )
    bug_id = bd_create(beads_project, "fix pre-existing bug", alloy_recipe="tdd-loop")
    engine.beads.claim(bug_id)

    async def gate_ok(bead, diff: str):
        return True, "ok"

    fake_harnesses.reset_calls()
    fake_harnesses.configure(_bug_script())

    async with open_checkpointer(engine.paths.workflows_db) as checkpointer:
        parent_ctx = await _parent_context(engine, parent_id, parent_run_id, checkpointer)
        child_result = await engine.run_child(
            bug_id,
            parent=parent_ctx,
            merge_gate=gate_ok,
        )

    assert child_result.outcome == "done"
    child_run = engine.store.latest_run_for_bead(bug_id)
    assert child_run is not None
    return parent_id, parent_run_id, bug_id, child_run["run_id"]


# -- alloy status --json parent/child linkage --------------------------------


async def test_status_json_parent_lists_child_run_and_null_remediating_after_child_done(
    engine,
    beads_project,
    alloy_home,
    fake_harnesses,
):
    parent_id, parent_run_id, bug_id, child_run_id = await _parent_with_finished_child(
        engine,
        beads_project,
        fake_harnesses,
    )

    result = _invoke("status", parent_id, "--json", project=beads_project, alloy_home=alloy_home)

    assert result.exit_code == 0
    parent_row = _status_bead(json.loads(result.stdout), parent_id)
    assert parent_row["run_id"] == parent_run_id
    assert parent_row["children"] == [child_run_id]
    assert parent_row["remediating"] is None


async def test_status_json_child_run_shows_parent_run_id(
    engine,
    beads_project,
    alloy_home,
    fake_harnesses,
):
    parent_id, parent_run_id, bug_id, child_run_id = await _parent_with_finished_child(
        engine,
        beads_project,
        fake_harnesses,
    )

    result = _invoke("status", bug_id, "--json", project=beads_project, alloy_home=alloy_home)

    assert result.exit_code == 0
    child_row = _status_bead(json.loads(result.stdout), bug_id)
    assert child_row["run_id"] == child_run_id
    assert child_row["parent_run_id"] == parent_run_id


async def test_status_json_child_run_includes_parent_bead_id(
    engine,
    beads_project,
    alloy_home,
    fake_harnesses,
):
    parent_id, _, bug_id, _ = await _parent_with_finished_child(
        engine,
        beads_project,
        fake_harnesses,
    )

    result = _invoke("status", bug_id, "--json", project=beads_project, alloy_home=alloy_home)

    assert result.exit_code == 0
    child_row = _status_bead(json.loads(result.stdout), bug_id)
    assert child_row["parent_bead_id"] == parent_id


async def test_status_plain_table_shows_parent_column(
    engine,
    beads_project,
    alloy_home,
    fake_harnesses,
):
    parent_id, _, bug_id, _ = await _parent_with_finished_child(
        engine,
        beads_project,
        fake_harnesses,
    )

    result = _invoke("status", project=beads_project, alloy_home=alloy_home)

    assert result.exit_code == 0
    output = result.stdout
    assert "parent" in output
    assert parent_id in output
    assert bug_id in output
    parent_line = next(line for line in output.splitlines() if parent_id in line and bug_id not in line)
    child_line = next(line for line in output.splitlines() if bug_id in line)
    assert parent_line.strip().startswith("-") or "│ -" in parent_line or parent_line.lstrip().startswith("-")
    assert parent_id in child_line


# -- alloy logs <parent> aggregates child calls ------------------------------


async def test_logs_parent_includes_child_bead_id_and_runner_name(
    engine,
    beads_project,
    alloy_home,
    fake_harnesses,
):
    parent_id, _, bug_id, _ = await _parent_with_finished_child(
        engine,
        beads_project,
        fake_harnesses,
    )

    result = _invoke("logs", parent_id, project=beads_project, alloy_home=alloy_home)

    assert result.exit_code == 0
    output = result.stdout
    assert bug_id in output
    assert any(name in output for name in CHILD_RUNNER_NAMES)
