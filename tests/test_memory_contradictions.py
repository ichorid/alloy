"""Context role memory contradictions (alloy-4ef.8): tests only.

These tests encode the acceptance criteria for memory_contradictions on
CONTEXT_SCHEMA and ContextPacket, the context prompt, gather_context
recording alloy:review:contradiction:<key> entries with provenance, a bead
note, and leaving disputed human-owned memories untouched. They are expected
to fail until the implementation lands.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import (
    FAKE_BD_SOURCE,
    FAKE_RUNNERS,
    FAKE_SOURCE,
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    write_tests_entry,
)
from support import make_harness

from alloy.beads import BeadsClient
from alloy.models import ContextPacket, parse_provenance
from alloy.recipes.tdd_loop import CONTEXT_SCHEMA, context_prompt

HUMAN_MEMORY_KEY = "conv"
HUMAN_MEMORY_VALUE = "repo uses pathlib"
CONTRADICTION_WHY = "repo uses os.path"
CONTRADICTION_MEMORY_KEY = f"alloy:review:contradiction:{HUMAN_MEMORY_KEY}"


def _context_with_contradictions(memory_contradictions: list[str]) -> dict:
    entry = context_entry()
    entry["structured"]["memory_contradictions"] = memory_contradictions
    return entry


def _workflow_script(**overrides):
    base = {
        "context": _context_with_contradictions(
            [
                f"{HUMAN_MEMORY_KEY}: {CONTRADICTION_WHY}",
                "ghost: nope",
            ],
        ),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "judge": [judge_entry("done")],
        "critic": critic_entry(),
        "synthesize": synthesize_entry(),
    }
    base.update(overrides)
    return base


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

    @property
    def calls(self) -> list[dict]:
        path = self.workdir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


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


def _bd_remember_calls(fake_workflow: FakeWorkflow) -> list[dict]:
    return [call for call in fake_workflow.calls if call.get("command") == "remember"]


def _bd_note_calls(fake_workflow: FakeWorkflow) -> list[dict]:
    return [call for call in fake_workflow.calls if call.get("command") == "note"]


def _remember_key_and_body(call: dict) -> tuple[str, str]:
    argv = call["argv"]
    key = argv[argv.index("--key") + 1]
    return key, argv[1]


def _is_contradiction_remember(call: dict) -> bool:
    key, _ = _remember_key_and_body(call)
    return key.startswith("alloy:review:contradiction:")


def _contradiction_remember_calls(fake_workflow: FakeWorkflow) -> list[dict]:
    return [call for call in _bd_remember_calls(fake_workflow) if _is_contradiction_remember(call)]


def _contradiction_note_calls(fake_workflow: FakeWorkflow) -> list[dict]:
    return [
        call
        for call in _bd_note_calls(fake_workflow)
        if "contradiction" in call["argv"][2].lower() or HUMAN_MEMORY_KEY in call["argv"][2]
    ]


# ---------------------------------------------------------------------------
# Schema, model, and prompt contract
# ---------------------------------------------------------------------------


def test_context_schema_includes_memory_contradictions():
    props = CONTEXT_SCHEMA["properties"]
    assert "memory_contradictions" in props
    assert props["memory_contradictions"]["type"] == "array"
    assert props["memory_contradictions"]["items"]["type"] == "string"


def test_context_prompt_asks_for_memory_contradictions():
    prompt = context_prompt("task brief", "acceptance text")
    assert "memory_contradictions" in prompt
    assert "key: why" in prompt


def test_context_packet_compact_caps_contradictions_at_five():
    contradictions = [f"k{i}: reason {i}" for i in range(7)]
    packet = ContextPacket(
        summary="summary",
        memory_contradictions=contradictions,
    )
    compacted = packet.compact()["memory_contradictions"]
    assert len(compacted) == 5
    assert compacted == contradictions[:5]


# ---------------------------------------------------------------------------
# Workflow: gather_context records contradictions for existing keys only
# ---------------------------------------------------------------------------


async def test_context_flags_memory_contradiction_for_existing_key_only(
    project,
    alloy_home,
    fake_workflow,
):
    """One existing-key contradiction -> one review remember, one note, conv untouched."""
    fake_workflow.configure(
        _workflow_script(),
        memories={HUMAN_MEMORY_KEY: HUMAN_MEMORY_VALUE},
    )
    beads = BeadsClient(repo=project, binary=str(fake_workflow.bd))
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        await harness.start()
    finally:
        harness.close()

    contradiction_remembers = _contradiction_remember_calls(fake_workflow)
    assert len(contradiction_remembers) == 1

    key, stored = _remember_key_and_body(contradiction_remembers[0])
    assert key == CONTRADICTION_MEMORY_KEY
    body, run_id, bead_id, at = parse_provenance(stored)
    assert CONTRADICTION_WHY in body
    assert run_id == harness.run_id
    assert bead_id == harness.bead.id
    assert at is not None

    human_key_remembers = [
        call
        for call in _bd_remember_calls(fake_workflow)
        if HUMAN_MEMORY_KEY in call.get("argv", []) and not _is_contradiction_remember(call)
    ]
    assert human_key_remembers == []

    assert beads.memories()[HUMAN_MEMORY_KEY] == HUMAN_MEMORY_VALUE

    contradiction_notes = _contradiction_note_calls(fake_workflow)
    assert len(contradiction_notes) == 1
