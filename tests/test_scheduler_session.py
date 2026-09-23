"""Scheduler session file: scheduler.json with started_at/ended_at and read_session()."""

from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from alloy import scheduler as scheduler_module
from alloy.engine import Engine, RunResult
from alloy.paths import AlloyPaths
from alloy.scheduler import Scheduler, read_pid
from conftest import bd_create


def read_session(paths: AlloyPaths) -> dict | None:
    return scheduler_module.read_session(paths)


@pytest.fixture
def scheduler(beads_project, alloy_home):
    return Scheduler(engine=Engine.open(beads_project, alloy_home), poll_seconds=0.01, once=True)


async def test_serve_writes_scheduler_session_with_timestamps(scheduler):
    """After serve() finishes, scheduler.json records pid and ISO started/ended times."""
    await scheduler.serve()

    assert read_pid(scheduler.pidfile) is None
    session = read_session(scheduler.engine.paths)
    assert session is not None
    assert set(session.keys()) == {"pid", "started_at", "ended_at"}
    assert session["pid"] == os.getpid()
    started = datetime.fromisoformat(session["started_at"])
    ended = datetime.fromisoformat(session["ended_at"])
    assert ended >= started


async def test_serve_scheduler_session_has_null_ended_at_while_running(
    scheduler, beads_project, monkeypatch,
):
    """While serve() is active, scheduler.json has ended_at null."""
    bd_create(beads_project, "task", alloy_recipe="tdd-loop")
    observed: dict = {}

    async def stub_run(bead_id: str, *, recipe_name: str | None = None):
        observed["session"] = read_session(scheduler.engine.paths)
        return RunResult(bead_id=bead_id, run_id="stub-run", outcome="done")

    monkeypatch.setattr(scheduler.engine, "run", stub_run)

    await scheduler.serve()

    session = observed.get("session")
    assert session is not None
    assert set(session.keys()) == {"pid", "started_at", "ended_at"}
    assert session["pid"] == os.getpid()
    assert session["ended_at"] is None
    datetime.fromisoformat(session["started_at"])


def test_read_session_missing_file_returns_none(alloy_home):
    paths = AlloyPaths.resolve(alloy_home).ensure()
    assert read_session(paths) is None


def test_read_session_unparsable_file_returns_none(alloy_home):
    paths = AlloyPaths.resolve(alloy_home).ensure()
    paths.scheduler_session.write_text("garbage", encoding="utf-8")
    assert read_session(paths) is None


def test_read_session_valid_file_returns_session_dict(alloy_home):
    paths = AlloyPaths.resolve(alloy_home).ensure()
    payload = {
        "pid": 42,
        "started_at": "2026-09-23T10:00:00+00:00",
        "ended_at": "2026-09-23T10:05:00+00:00",
    }
    paths.scheduler_session.write_text(json.dumps(payload), encoding="utf-8")

    session = read_session(paths)

    assert session is not None
    assert set(session.keys()) == {"pid", "started_at", "ended_at"}
    assert session == payload
