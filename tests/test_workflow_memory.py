"""Workflow routing, limits, consilium and the human gate.

Every test scripts the agents' decisions and then asserts on what Alloy did with
them -- especially where Alloy overrules the agent.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import date
from pathlib import Path

import pytest
from conftest import (
    FAKE_BD_SOURCE,
    FAKE_RUNNERS,
    FAKE_SOURCE,
    context_entry,
    estimate_entry,
    harvest_entry,
    implement_entry,
    judge_entry,
    verifier_run_entry,
    verifier_stop_entry,
)
from support import make_bead, make_harness
from workflow_support import (
    AUTODETECT_PYTEST,
    CHECK_HINTS_KEY,
    HINTS_HEADING,
    REGRESSION_PYTEST,
    TARGETED_PYTEST,
    FakeWorkflow,
    script,
    verification_script,
)

from alloy.beads import BeadsClient
from alloy.config import MemorySpec
from alloy.memory_embed import render_embed_block
from alloy.models import (
    EMBED_KEY,
    EMBED_STALE_KEY,
    LAST_REVIEW_KEY,
    ProjectMemory,
    parse_provenance,
    with_provenance,
)


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


def _workflow_beads(project: Path, fake_workflow: FakeWorkflow) -> BeadsClient:
    return BeadsClient(repo=project, binary=str(fake_workflow.bd))


def _bd_remember_calls(fake_workflow: FakeWorkflow) -> list[dict]:
    return [call for call in fake_workflow.calls if call.get("command") == "remember"]


def _check_hints_remember_calls(fake_workflow: FakeWorkflow) -> list[dict]:
    return [call for call in _bd_remember_calls(fake_workflow) if CHECK_HINTS_KEY in call.get("argv", [])]


def _remember_key_and_body(call: dict) -> tuple[str, str]:
    argv = call["argv"]
    key = argv[argv.index("--key") + 1]
    return key, argv[1]


def _expected_check_hints_body() -> str:
    """Runnable verifier checks, newest first, excluding exit-127 commands."""
    return f"regression: {REGRESSION_PYTEST}\ntargeted: {TARGETED_PYTEST}"


def _check_hints_done_script(**overrides):
    base = verification_script(
        context=context_entry(check_hints=[]),
        implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
        verifier=[
            verifier_run_entry(TARGETED_PYTEST, kind="targeted"),
            verifier_run_entry(TARGETED_PYTEST, kind="targeted"),
            verifier_run_entry(REGRESSION_PYTEST, kind="regression"),
            verifier_run_entry("definitely-not-a-program"),
            verifier_stop_entry("runnable checks recorded"),
        ],
        judge=[
            judge_entry("retry", "targeted check still red"),
            judge_entry("done"),
        ],
    )
    base.update(overrides)
    return base


def _hints_section(prompt: str) -> str:
    start = prompt.index(HINTS_HEADING)
    rest = prompt[start:]
    next_heading = rest.find("\n## ", len(HINTS_HEADING))
    return rest[:next_heading] if next_heading != -1 else rest


async def test_done_run_remembers_runnable_verifier_checks_as_alloy_check_hints(project, alloy_home, fake_workflow):
    """DONE finish writes alloy:check-hints once with runnable verifier commands only."""
    fake_workflow.configure(_check_hints_done_script())
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    remembers = _check_hints_remember_calls(fake_workflow)
    assert len(remembers) == 1

    _, stored = _remember_key_and_body(remembers[0])
    body, run_id, bead_id, at = parse_provenance(stored)
    assert run_id == harness.run_id
    assert bead_id == harness.bead.id
    assert at is not None
    assert body == _expected_check_hints_body()
    assert "definitely-not-a-program" not in body


async def test_failed_run_writes_no_alloy_check_hints(project, alloy_home, fake_workflow):
    """FAILED runs must not persist alloy:check-hints even when checks ran."""
    fake_workflow.configure(
        _check_hints_done_script(judge=[judge_entry("abort", "cannot finish")]),
    )
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "failed"
    assert _check_hints_remember_calls(fake_workflow) == []


async def test_done_run_skips_remember_when_alloy_check_hints_unchanged(project, alloy_home, fake_workflow):
    """A second DONE run with the same checks does not rewrite alloy:check-hints."""
    fake_workflow.configure(_check_hints_done_script())
    beads = _workflow_beads(project, fake_workflow)
    first_harness = make_harness(project, alloy_home, beads=beads, run_id="run-first")
    try:
        first_final = await first_harness.start()
    finally:
        first_harness.close()

    assert first_final["outcome"] == "done"
    first_remembers = _check_hints_remember_calls(fake_workflow)
    assert len(first_remembers) == 1
    _, first_body = _remember_key_and_body(first_remembers[0])
    first_stripped, _, _, _ = parse_provenance(first_body)

    fake_workflow.configure(
        _check_hints_done_script(),
        memories={CHECK_HINTS_KEY: first_body},
    )
    # A fresh bead gets a fresh worktree: the first run's fix already lives in
    # t-1's worktree, so a second run there would find its baseline green.
    second_harness = make_harness(
        project,
        alloy_home,
        bead=make_bead("t-2"),
        beads=beads,
        run_id="run-second",
    )
    try:
        second_final = await second_harness.start()
    finally:
        second_harness.close()

    assert second_final["outcome"] == "done"
    assert first_stripped == _expected_check_hints_body()
    # configure() reset calls.jsonl before the second run: it must log no write.
    assert _check_hints_remember_calls(fake_workflow) == []


async def test_verifier_prompt_lists_remembered_check_hints_before_autodetect(project, alloy_home, fake_workflow):
    """alloy:check-hints commands precede autodetected hints in the verifier prompt."""
    fake_workflow.configure(
        script(
            context=context_entry(check_hints=[]),
            verifier=[
                verifier_run_entry(REGRESSION_PYTEST, kind="regression"),
                verifier_stop_entry("regression green"),
            ],
        ),
        memories={CHECK_HINTS_KEY: _expected_check_hints_body()},
    )
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        await harness.start()
    finally:
        harness.close()

    verifier_prompt_text = fake_workflow.calls_for("verifier")[0]["prompt"]
    hints = _hints_section(verifier_prompt_text)
    targeted_pos = hints.index(TARGETED_PYTEST)
    regression_pos = hints.index(REGRESSION_PYTEST)
    autodetect_pos = hints.index(AUTODETECT_PYTEST)
    assert targeted_pos < autodetect_pos
    assert regression_pos < autodetect_pos


# -- alloy:calibration persistence (alloy-4ef.12) -----------------------------


def _calibration_imports():
    from alloy.models import (
        CALIBRATION_KEY,
        format_calibration,
        update_calibration,
    )

    return CALIBRATION_KEY, format_calibration, update_calibration


def _calibration_remember_calls(fake_workflow: FakeWorkflow) -> list[dict]:
    calibration_key, _, _ = _calibration_imports()
    return [call for call in _bd_remember_calls(fake_workflow) if calibration_key in call.get("argv", [])]


def _medium_calibration_body() -> str:
    _, _, update_calibration = _calibration_imports()
    body = update_calibration("", level="medium", iterations=3, agent_calls=7, overrun=False)
    return update_calibration(body, level="medium", iterations=3, agent_calls=7, overrun=False)


def _calibration_done_script(**overrides):
    base = script(estimate=estimate_entry(complexity="medium"))
    base.update(overrides)
    return base


async def test_finish_remembers_calibration_as_alloy_calibration(project, alloy_home, fake_workflow):
    """Finish writes alloy:calibration once with per-level aggregate JSON."""
    calibration_key, _, _ = _calibration_imports()
    fake_workflow.configure(_calibration_done_script())
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    remembers = _calibration_remember_calls(fake_workflow)
    assert len(remembers) == 1

    key, stored = _remember_key_and_body(remembers[0])
    assert key == calibration_key
    body, run_id, bead_id, at = parse_provenance(stored)
    assert run_id == harness.run_id
    assert bead_id == harness.bead.id
    assert at is not None
    assert json.loads(body)["medium"]["runs"] == 1


async def test_estimate_prompt_shows_calibration_from_memory(project, alloy_home, fake_workflow):
    """Stored alloy:calibration renders as one line in the estimate project layer."""
    calibration_key, format_calibration, _ = _calibration_imports()
    seeded = _medium_calibration_body()
    line = format_calibration(seeded)
    fake_workflow.configure(
        _calibration_done_script(),
        memories={calibration_key: seeded},
    )
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        await harness.start()
    finally:
        harness.close()

    estimate_prompt_text = fake_workflow.calls_for("estimate")[0]["prompt"]
    assert estimate_prompt_text.count("calibration:") == 1
    assert line in estimate_prompt_text


# -- harvest lesson persistence (alloy-4ef.10) --------------------------------


HARVEST_LESSON_KEY = "lesson-x"
LESSON_MEMORY_KEY = f"alloy:lesson:{HARVEST_LESSON_KEY}"
LESSON_BODY = "Always verify slugify with targeted tests before the full suite."


def _lesson_remember_calls(fake_workflow: FakeWorkflow) -> list[dict]:
    return [call for call in _bd_remember_calls(fake_workflow) if LESSON_MEMORY_KEY in call.get("argv", [])]


def _bd_note_calls(fake_workflow: FakeWorkflow) -> list[dict]:
    return [call for call in fake_workflow.calls if call.get("command") == "note"]


def _harvest_two_iteration_script(**overrides):
    base = script(
        implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
        judge=[
            judge_entry("retry", "targeted check still red"),
            judge_entry("done"),
        ],
        harvest=harvest_entry(
            scope="repo",
            key=HARVEST_LESSON_KEY,
            lesson=LESSON_BODY,
            confidence=0.9,
        ),
    )
    base.update(overrides)
    return base


async def test_done_two_iteration_run_remembers_repo_lesson_with_provenance(project, alloy_home, fake_workflow):
    """High-confidence repo harvest after DONE with iteration>=2 writes one lesson."""
    fake_workflow.configure(_harvest_two_iteration_script())
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["iteration"] == 2
    assert len(fake_workflow.calls_for("harvest")) == 1
    remembers = _lesson_remember_calls(fake_workflow)
    assert len(remembers) == 1

    key, stored = _remember_key_and_body(remembers[0])
    assert key == LESSON_MEMORY_KEY
    body, run_id, bead_id, at = parse_provenance(stored)
    assert run_id == harness.run_id
    assert bead_id == harness.bead.id
    assert at is not None
    assert body == LESSON_BODY

    notes = [note for note in _bd_note_calls(fake_workflow) if HARVEST_LESSON_KEY in note["argv"][2]]
    assert len(notes) == 1


async def test_harvest_task_scope_writes_no_lesson(project, alloy_home, fake_workflow):
    """scope 'task' must not persist alloy:lesson:* even after two iterations."""
    fake_workflow.configure(
        _harvest_two_iteration_script(
            harvest=harvest_entry(scope="task", confidence=0.9),
        ),
    )
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert len(fake_workflow.calls_for("harvest")) == 1
    assert _lesson_remember_calls(fake_workflow) == []


async def test_harvest_low_confidence_writes_no_lesson(project, alloy_home, fake_workflow):
    """confidence below harvest_min_confidence must not write a repo lesson."""
    fake_workflow.configure(
        _harvest_two_iteration_script(
            harvest=harvest_entry(scope="repo", confidence=0.5),
        ),
    )
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert len(fake_workflow.calls_for("harvest")) == 1
    assert _lesson_remember_calls(fake_workflow) == []


async def test_done_single_iteration_run_writes_no_lesson(project, alloy_home, fake_workflow):
    """A DONE run with only one implement iteration must not harvest a lesson."""
    fake_workflow.configure(
        script(
            harvest=harvest_entry(scope="repo", confidence=0.9),
        ),
    )
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["iteration"] == 1
    assert len(fake_workflow.calls_for("harvest")) == 1
    assert _lesson_remember_calls(fake_workflow) == []


async def test_harvest_runner_failure_writes_no_lesson_and_outcome_stays_done(project, alloy_home, fake_workflow):
    """A failed harvest call must not write memory or change the run outcome."""
    fake_workflow.configure(
        _harvest_two_iteration_script(
            harvest={"exit": 1, "is_error": True, "text": "harvest runner failed"},
        ),
    )
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["iteration"] == 2
    assert len(fake_workflow.calls_for("harvest")) == 1
    assert _lesson_remember_calls(fake_workflow) == []


# -- embed block staleness at run start (alloy-4ef.20) ------------------------


EMBED_RUN_ID = "run-embed-stale-1"
EMBED_BEAD_ID = "alloy-4ef.20"
EMBED_LESSON_KEY = "alloy:lesson:embed-stale"
EMBED_LESSON_BODY = "Read memory once at run start"
EMBED_HUMAN_KEY = "conv"
EMBED_HUMAN_VALUE = "repo uses pathlib"
EMBED_LAST_REVIEW = date(2026, 9, 22)
EMBED_INITIAL_AGENTS = "# Agents\n\nFollow these rules.\n"


def _embed_run_memories() -> dict[str, str]:
    return {
        EMBED_KEY: json.dumps([EMBED_HUMAN_KEY, EMBED_LESSON_KEY]),
        LAST_REVIEW_KEY: EMBED_LAST_REVIEW.isoformat(),
        EMBED_HUMAN_KEY: EMBED_HUMAN_VALUE,
        EMBED_LESSON_KEY: with_provenance(
            EMBED_LESSON_BODY,
            EMBED_RUN_ID,
            EMBED_BEAD_ID,
            date(2026, 9, 20),
        ),
    }


def _embed_project_memory() -> ProjectMemory:
    return ProjectMemory.from_raw(_embed_run_memories(), MemorySpec())


def _worktree_agents(harness) -> Path:
    return harness.worktrees.ensure(harness.bead.id).path / "AGENTS.md"


def _write_worktree_agents(harness, file_text: str) -> Path:
    agents = _worktree_agents(harness)
    agents.parent.mkdir(parents=True, exist_ok=True)
    agents.write_text(file_text, encoding="utf-8")
    return agents


def _fresh_embed_agents_text() -> str:
    managed = render_embed_block(_embed_project_memory(), MemorySpec())
    return f"{EMBED_INITIAL_AGENTS}\n{managed}\n"


def _stale_embed_agents_text() -> str:
    managed = render_embed_block(_embed_project_memory(), MemorySpec())
    stale_managed = managed.replace(EMBED_LESSON_BODY, "hand-edited stale lesson body")
    return f"{EMBED_INITIAL_AGENTS}\n{stale_managed}\n"


def _embed_stale_remember_calls(fake_workflow: FakeWorkflow) -> list[dict]:
    return [call for call in _bd_remember_calls(fake_workflow) if EMBED_STALE_KEY in call.get("argv", [])]


def _embed_stale_note_calls(fake_workflow: FakeWorkflow) -> list[dict]:
    return [
        call
        for call in _bd_note_calls(fake_workflow)
        if any("embed-stale" in str(part) for part in call.get("argv", []))
    ]


async def test_run_start_flags_stale_embed_block_with_one_remember_and_note(
    project,
    alloy_home,
    fake_workflow,
):
    """A stale managed block in the worktree sets alloy:meta:embed-stale once at run start."""
    fake_workflow.configure(script(), memories=_embed_run_memories())
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    _write_worktree_agents(harness, _stale_embed_agents_text())
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    remembers = _embed_stale_remember_calls(fake_workflow)
    assert len(remembers) == 1
    key, body = _remember_key_and_body(remembers[0])
    assert key == EMBED_STALE_KEY
    assert body == "true"
    assert len(_embed_stale_note_calls(fake_workflow)) == 1


async def test_run_start_writes_no_embed_stale_flag_when_block_is_fresh(
    project,
    alloy_home,
    fake_workflow,
):
    """A matching managed block must not set alloy:meta:embed-stale at run start."""
    fake_workflow.configure(script(), memories=_embed_run_memories())
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    _write_worktree_agents(harness, _fresh_embed_agents_text())
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert _embed_stale_remember_calls(fake_workflow) == []
    assert _embed_stale_note_calls(fake_workflow) == []


async def test_run_start_writes_no_embed_stale_flag_when_instruction_file_has_no_block(
    project,
    alloy_home,
    fake_workflow,
):
    """Instruction files without a managed block are skipped at run start."""
    fake_workflow.configure(script(), memories=_embed_run_memories())
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    _write_worktree_agents(harness, EMBED_INITIAL_AGENTS)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert _embed_stale_remember_calls(fake_workflow) == []
    assert _embed_stale_note_calls(fake_workflow) == []


# -- alloy:regression in consilium evidence packet (alloy-4ef.13) -------------


REGRESSION_PREFIX_KEY = "alloy:regression:src"
REGRESSION_TITLE = "verify() mishandles empty input"
REGRESSION_BODY = with_provenance(
    REGRESSION_TITLE,
    "prior-run-id",
    "bug-bead-id",
    date(2026, 9, 20),
)
KNOWN_REGRESSION_HEADING = "## Known regression areas"


def _consilium_for_regression(**overrides):
    base = script(
        implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
        judge=[judge_entry("consilium", "stuck"), judge_entry("done")],
    )
    base.update(overrides)
    return base


def _context_with_relevant_files(files: list[str]) -> dict:
    entry = context_entry()
    entry["structured"]["relevant_files"] = files
    return entry


async def test_consilium_evidence_packet_lists_matching_regression_areas(
    project,
    alloy_home,
    fake_workflow,
):
    """Critics see regression memories whose prefix matches a relevant_files path."""
    fake_workflow.configure(
        _consilium_for_regression(
            context=_context_with_relevant_files(["src/alloy/x.py"]),
        ),
        memories={REGRESSION_PREFIX_KEY: REGRESSION_BODY},
    )
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["consiliums"] == 1
    critic_prompts = [call["prompt"] for call in fake_workflow.calls_for("critic")]
    assert critic_prompts
    for prompt in critic_prompts:
        assert KNOWN_REGRESSION_HEADING in prompt
        assert REGRESSION_TITLE in prompt


async def test_consilium_evidence_packet_omits_regression_areas_without_prefix_match(
    project,
    alloy_home,
    fake_workflow,
):
    """No regression section when relevant_files share no top-level prefix."""
    fake_workflow.configure(
        _consilium_for_regression(
            context=_context_with_relevant_files(["docs/a.md"]),
        ),
        memories={REGRESSION_PREFIX_KEY: REGRESSION_BODY},
    )
    beads = _workflow_beads(project, fake_workflow)
    harness = make_harness(project, alloy_home, beads=beads)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["consiliums"] == 1
    for prompt in [call["prompt"] for call in fake_workflow.calls_for("critic")]:
        assert KNOWN_REGRESSION_HEADING not in prompt
        assert REGRESSION_TITLE not in prompt
