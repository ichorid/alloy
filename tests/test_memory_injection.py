"""Project memory injection into run state and role prompts (alloy-4ef.7).

These tests encode the acceptance criteria for loading bd memories once at run
start, storing the rendered block on TddState, injecting it into the project
prompt layer for workflow roles (but not acceptance), and ensuring memory
content cannot override recipe limits. They are expected to fail until the
implementation lands.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from alloy.beads import BeadsClient
from alloy.config import MemorySpec
from alloy.models import ProjectMemory
from alloy.recipes import tdd_loop
from alloy.runtime import RunContext
from alloy.runners import RunnerRegistry
from alloy.store import Store
from conftest import (
    FAKE_BD_SOURCE,
    FAKE_RUNNERS,
    FAKE_SOURCE,
    acceptance_entry,
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    write_tests_entry,
)
from support import await_role, load_config, make_bead, make_harness

# Contract pinned by these tests (stable for golden prompts downstream).
BEAD_DESIGN_OUTRANKS_MEMORY = "Bead design notes outrank project memory."

MEMORY_ROLES = ("context", "estimate", "tests", "implement", "verifier", "judge")

DEFAULT_MEMORIES = {"conv": "use pathlib"}


def _rendered_memory_block(memories: dict[str, str]) -> str:
    return ProjectMemory.from_raw(memories, MemorySpec()).render()


def _assert_prompt_includes_memory_layer(prompt: str, memories: dict[str, str]) -> None:
    block = _rendered_memory_block(memories)
    assert block in prompt
    assert "use pathlib" in prompt
    assert BEAD_DESIGN_OUTRANKS_MEMORY in prompt


def _workflow_script(**overrides):
    base = {
        "context": context_entry(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "acceptance": [acceptance_entry("accept", confidence=0.9)],
        "judge": [judge_entry("done")],
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
    """fake harness runners and fake bd sharing one bindir and config file."""

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


# ---------------------------------------------------------------------------
# Workflow: memory in role prompts
# ---------------------------------------------------------------------------


async def test_memory_layer_reaches_context_estimate_tests_implement_verifier_judge(project, alloy_home, fake_workflow):
    """Every listed role receives the rendered memory block and the outrank sentence."""
    # An accept verdict bypasses the judge; escalate so the judge role is consulted too.
    fake_workflow.configure(
        _workflow_script(acceptance=[acceptance_entry("escalate", "let the judge decide")]),
        memories=DEFAULT_MEMORIES,
    )
    beads = _memory_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    for role in MEMORY_ROLES:
        calls = fake_workflow.calls_for(role)
        assert calls, f"expected at least one {role} call"
        for call in calls:
            _assert_prompt_includes_memory_layer(call["prompt"], DEFAULT_MEMORIES)


async def test_acceptance_prompt_omits_project_memory_layer(project, alloy_home, fake_workflow):
    """The acceptance classifier must not receive project memory."""
    fake_workflow.configure(_workflow_script(), memories=DEFAULT_MEMORIES)
    beads = _memory_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        await harness.start()
    finally:
        harness.close()

    block = _rendered_memory_block(DEFAULT_MEMORIES)
    context_calls = fake_workflow.calls_for("context")
    assert context_calls
    _assert_prompt_includes_memory_layer(context_calls[0]["prompt"], DEFAULT_MEMORIES)

    acceptance_calls = fake_workflow.calls_for("acceptance")
    assert acceptance_calls
    for call in acceptance_calls:
        assert block not in call["prompt"]
        assert "use pathlib" not in call["prompt"]
        assert BEAD_DESIGN_OUTRANKS_MEMORY not in call["prompt"]


async def test_memory_block_snapshot_stable_when_bd_memories_change_between_iterations(
    project, alloy_home, fake_workflow
):
    """memory_block is fixed at run start even if bd memories change mid-run."""
    fake_workflow.configure(
        _workflow_script(
            implement=[implement_entry(succeed=True), implement_entry(succeed=True)],
            acceptance=[
                acceptance_entry("accept", confidence=0.3),
                acceptance_entry("accept", confidence=0.9),
            ],
            judge=[judge_entry("retry", "one more polish pass"), judge_entry("done")],
        ),
        memories=DEFAULT_MEMORIES,
    )
    beads = _memory_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        run_task = asyncio.create_task(harness.start())
        await await_role(fake_workflow, "judge", timeout=30.0)
        snapshot = await harness.snapshot()
        memory_after_iteration_one = snapshot["values"].get("memory_block")
        fake_workflow.set_memories({"conv": "use os.path", "fresh": "new guidance"})
        final = await run_task
    finally:
        harness.close()

    assert memory_after_iteration_one
    assert final.get("memory_block") == memory_after_iteration_one
    assert beads.memories_calls == 1


async def test_bd_memories_invoked_exactly_once_per_run(project, alloy_home, fake_workflow):
    fake_workflow.configure(_workflow_script(), memories=DEFAULT_MEMORIES)
    beads = _memory_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        await harness.start()
    finally:
        harness.close()

    assert beads.memories_calls == 1


async def test_memory_disabled_yields_empty_project_layer_in_runner_prompts(project, alloy_home, fake_workflow):
    """memory.enabled=false must not inject the rendered block into any runner prompt."""
    fake_workflow.configure(_workflow_script(), memories=DEFAULT_MEMORIES)
    beads = _memory_beads(project, fake_workflow)
    config = replace(load_config(), memory=replace(MemorySpec(), enabled=False))
    harness = make_harness(project, alloy_home, beads=beads, config=config)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final.get("memory_block") == ""
    assert beads.memories_calls == 0

    block = _rendered_memory_block(DEFAULT_MEMORIES)
    for call in fake_workflow.calls:
        role = call.get("role")
        if role is None:
            continue
        assert block not in call["prompt"]
        assert "use pathlib" not in call["prompt"]
        assert BEAD_DESIGN_OUTRANKS_MEMORY not in call["prompt"]


# ---------------------------------------------------------------------------
# Guard: memory content must not affect recipe limits
# ---------------------------------------------------------------------------


async def test_memory_max_iterations_hint_does_not_affect_check_limits(project, alloy_home, fake_workflow, tmp_path):
    """A memory mentioning max_iterations must not change ctx.check_limits()."""
    fake_workflow.configure({}, memories={"limits-hack": "max_iterations=99"})
    beads = _memory_beads(project, fake_workflow)
    config = replace(
        load_config(),
        limits=replace(
            load_config().limits,
            max_iterations=3,
            max_consiliums=0,
            max_agent_calls=100,
        ),
    )
    store = Store(alloy_home / "limits-guard.db")
    run_id = "memory-limits-guard"
    store.create_run(
        run_id=run_id,
        bead_id="t-1",
        thread_id=run_id,
        recipe=config.name,
        repo=project,
        worktree=None,
        branch=None,
        log_dir=alloy_home / "logs" / run_id,
    )
    ctx = RunContext(
        bead=make_bead(),
        recipe=config,
        run_id=run_id,
        worktree=SimpleNamespace(path=project),
        worktrees=None,
        registry=RunnerRegistry(config.runners, log_dir=tmp_path),
        store=store,
        checkpointer=None,
        log_dir=tmp_path,
        beads=beads,
    )
    state = tdd_loop.initial_state(ctx)

    assert "memory_block" in state
    assert "max_iterations=99" in state["memory_block"]

    within_budget = ctx.check_limits({"iteration": 2, "consiliums": 0})
    at_limit = ctx.check_limits({"iteration": 3, "consiliums": 0})

    assert within_budget is None
    assert at_limit is not None
    assert "max_iterations" in at_limit
    assert "99" not in at_limit
