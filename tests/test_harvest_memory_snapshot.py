"""Harvest memory snapshot (alloy-muh): tests only.

These tests encode the acceptance criteria that harvest must reuse the
run-start ``memory_lessons`` snapshot from graph state instead of calling
``RunContext.project_memory()`` (which re-invokes ``bd memories`` and breaks
the once-per-run invariant from alloy-4ef.7). They are expected to fail until
the harvest path reads lessons from state.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import (
    FAKE_BD_SOURCE,
    FAKE_RUNNERS,
    FAKE_SOURCE,
    acceptance_entry,
    context_entry,
    critic_entry,
    harvest_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    write_tests_entry,
)
from support import await_role, load_config, make_bead, make_harness

from alloy.beads import BeadsClient
from alloy.models import LESSON_KEY_PREFIX, with_provenance
from alloy.recipes import tdd_loop
from alloy.runners import RunnerRegistry
from alloy.runtime import RunContext
from alloy.store import Store

RUN_ID = "harvest-snapshot-run"
BEAD_ID = "alloy-muh"

SNAPSHOTTED_LESSON_KEY = f"{LESSON_KEY_PREFIX}slugify"
SNAPSHOTTED_LESSON_BODY = "Always verify slugify with targeted tests first."
REPLACED_LESSON_BODY = "Live bd lesson replaced mid-run."
DEFAULT_MEMORIES = {"conv": "use pathlib"}


def _lesson_memories(body: str = SNAPSHOTTED_LESSON_BODY) -> dict[str, str]:
    return {
        **DEFAULT_MEMORIES,
        SNAPSHOTTED_LESSON_KEY: with_provenance(
            body,
            RUN_ID,
            BEAD_ID,
            date(2026, 9, 23),
        ),
    }


def _workflow_script(**overrides):
    base = {
        "context": context_entry(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=False), implement_entry(succeed=True)],
        "acceptance": [
            acceptance_entry("accept", confidence=0.3),
            acceptance_entry("accept", confidence=0.9),
        ],
        "judge": [
            judge_entry("retry", "one more polish pass"),
            judge_entry("done"),
        ],
        "harvest": harvest_entry(scope="repo", confidence=0.9),
        "critic": critic_entry(),
        "synthesize": synthesize_entry(),
    }
    base.update(overrides)
    return base


class CountingBeadsClient:
    """Wraps BeadsClient and records how often memories() is invoked."""

    def __init__(self, inner: BeadsClient) -> None:
        self._inner = inner
        self.memories_calls = 0

    def memories(self) -> dict[str, str]:
        self.memories_calls += 1
        return self._inner.memories()

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class FakeWorkflow:
    """Fake harness runners and fake bd sharing one bindir and config file."""

    def __init__(self, bindir: Path, workdir: Path, config_path: Path) -> None:
        self.bindir = bindir
        self.workdir = workdir
        self.config_path = config_path

    @property
    def bd(self) -> Path:
        return self.bindir / "bd"

    def configure(
        self,
        agent_script: dict,
        *,
        memories: dict[str, str] | None = None,
    ) -> None:
        config = dict(agent_script)
        if memories is not None:
            config["memories"] = memories
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        (self.workdir / "calls.jsonl").unlink(missing_ok=True)
        (self.workdir / "counters.json").unlink(missing_ok=True)

    def set_memories(self, memories: dict[str, str]) -> None:
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["memories"] = memories
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

    @property
    def calls(self) -> list[dict]:
        path = self.workdir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def calls_for(self, role: str) -> list[dict]:
        return [call for call in self.calls if call.get("role") == role]


@pytest.fixture
def fake_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name in FAKE_RUNNERS:
        target = bindir / name
        shutil.copy(FAKE_SOURCE, target)
        target.chmod(0o755)
    bd_binary = bindir / "bd"
    shutil.copy(FAKE_BD_SOURCE, bd_binary)
    bd_binary.chmod(0o755)

    workdir = tmp_path / "fake-state"
    workdir.mkdir()
    config_path = workdir / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ALLOY_FAKE_DIR", str(workdir))
    monkeypatch.setenv("ALLOY_FAKE_CONFIG", str(config_path))

    return FakeWorkflow(bindir=bindir, workdir=workdir, config_path=config_path)


def _memory_beads(project: Path, fake_workflow: FakeWorkflow) -> CountingBeadsClient:
    return CountingBeadsClient(
        BeadsClient(repo=project, binary=str(fake_workflow.bd)),
    )


def _existing_lessons_section(prompt: str) -> str:
    marker = "## Existing repository lessons\n"
    tail = prompt.split(marker, 1)[1]
    return tail.split("\n##", 1)[0].strip()


def _make_run_context(
    project: Path,
    alloy_home: Path,
    beads: CountingBeadsClient,
    *,
    memories: dict[str, str],
) -> RunContext:
    fake_workflow_dir = Path(os.environ["ALLOY_FAKE_DIR"])
    config_path = Path(os.environ["ALLOY_FAKE_CONFIG"])
    config_path.write_text(json.dumps({"memories": memories}), encoding="utf-8")
    store = Store(alloy_home / "initial-state.db")
    store.create_run(
        run_id=RUN_ID,
        bead_id=BEAD_ID,
        thread_id=RUN_ID,
        recipe=load_config().name,
        repo=project,
        worktree=project,
        branch="alloy/test",
        log_dir=alloy_home / "logs" / RUN_ID,
    )
    return RunContext(
        bead=make_bead(bead_id=BEAD_ID),
        recipe=load_config(),
        run_id=RUN_ID,
        worktree=SimpleNamespace(path=project),
        worktrees=None,
        registry=RunnerRegistry(load_config().runners, log_dir=fake_workflow_dir),
        store=store,
        checkpointer=None,
        log_dir=fake_workflow_dir,
        beads=beads,
    )


def _harvest_function_body() -> str:
    from alloy.recipes import workflow_nodes

    return inspect.getsource(workflow_nodes._make_node_harvest)


def test_harvest_wires_existing_lessons_from_state_memory_lessons():
    """Harvest must pass state['memory_lessons'] into harvest_prompt, not project_memory()."""
    body = _harvest_function_body()
    assert 'state.get("memory_lessons")' in body or "state.get('memory_lessons')" in body
    assert "ctx.project_memory()" not in body


def test_initial_state_snapshots_lesson_bodies_in_memory_lessons(
    project,
    alloy_home,
    fake_workflow,
):
    """initial_state must stash provenance-stripped alloy:lesson:* bodies on state."""
    beads = _memory_beads(project, fake_workflow)
    ctx = _make_run_context(project, alloy_home, beads, memories=_lesson_memories())

    state = tdd_loop.initial_state(ctx)

    assert state["memory_lessons"] == {SNAPSHOTTED_LESSON_KEY: SNAPSHOTTED_LESSON_BODY}
    assert beads.memories_calls == 1


# ---------------------------------------------------------------------------
# Workflow: harvest must not re-invoke bd memories
# ---------------------------------------------------------------------------


async def test_harvest_uses_run_start_lessons_when_bd_lessons_change_before_harvest(
    project,
    alloy_home,
    fake_workflow,
):
    """Harvest prompt must list run-start lessons even if bd memories change late."""
    fake_workflow.configure(
        _workflow_script(),
        memories=_lesson_memories(),
    )
    beads = _memory_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        run_task = asyncio.create_task(harness.start())
        await await_role(fake_workflow, "judge", timeout=30.0)
        fake_workflow.set_memories(_lesson_memories(REPLACED_LESSON_BODY))
        final = await run_task
    finally:
        harness.close()

    harvest_calls = fake_workflow.calls_for("harvest")
    assert harvest_calls, "expected harvest to run after finish"
    lessons = _existing_lessons_section(harvest_calls[0]["prompt"])
    assert SNAPSHOTTED_LESSON_BODY in lessons
    assert REPLACED_LESSON_BODY not in lessons
    assert final["outcome"] == "done"
    assert beads.memories_calls == 1


async def test_harvest_respects_empty_memory_lessons_override_over_live_bd(
    project,
    alloy_home,
    fake_workflow,
):
    """Harvest must read memory_lessons from state, not live bd, when they differ."""
    fake_workflow.configure(
        _workflow_script(),
        memories=_lesson_memories(),
    )
    beads = _memory_beads(project, fake_workflow)
    harness = make_harness(
        project,
        alloy_home,
        beads=beads,
        initial_state_overrides={"memory_lessons": {}},
    )
    try:
        await harness.start()
    finally:
        harness.close()

    harvest_calls = fake_workflow.calls_for("harvest")
    assert harvest_calls
    lessons = _existing_lessons_section(harvest_calls[0]["prompt"])
    assert lessons == "(none)"
    assert beads.memories_calls == 1


async def test_bd_memories_invoked_once_when_harvest_reads_existing_lessons(
    project,
    alloy_home,
    fake_workflow,
):
    """A DONE run through harvest with stored lessons must hit bd memories once."""
    fake_workflow.configure(
        _workflow_script(),
        memories=_lesson_memories(),
    )
    beads = _memory_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        await harness.start()
    finally:
        harness.close()

    assert fake_workflow.calls_for("harvest")
    assert beads.memories_calls == 1
