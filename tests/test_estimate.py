"""Estimate step: complexity classification after context, before tests."""

from __future__ import annotations

import json
from dataclasses import replace

from alloy.config import RoleSpec, MemorySpec
from alloy.models import ProjectMemory
from alloy.prompts import LAYER_SEPARATOR
from alloy.recipes.tdd_loop import estimate_prompt
from conftest import (
    context_entry,
    critic_entry,
    estimate_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    write_tests_entry,
)
from support import load_config, make_bead, make_harness


def script(**overrides):
    base = {
        "context": context_entry(),
        "estimate": estimate_entry(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "judge": [judge_entry("done")],
        "critic": critic_entry(),
        "synthesize": synthesize_entry(),
    }
    base.update(overrides)
    return base


async def test_estimate_records_simple_complexity_in_state_store_and_ledger(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(script())
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["complexity"] == "simple"
    assert final["complexity_source"] == "estimate"

    run = harness.store.get_run(harness.run_id)
    assert run["complexity"] == "simple"

    calls = harness.store.agent_calls(harness.run_id)
    estimate_rows = [c for c in calls if c["role"] == "estimate"]
    assert len(estimate_rows) == 1
    roles = [c["role"] for c in calls]
    assert roles.index("estimate") < roles.index("tests")

    implement_row = next(c for c in calls if c["role"] == "implement")
    assert implement_row["runner"] == "cursor"
    assert implement_row["model"] == "composer-2.5"


async def test_alloy_complexity_metadata_skips_estimate_agent_call(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(script())
    bead = make_bead(
        metadata={
            "alloy_recipe": "tdd-loop",
            "alloy_complexity": "complex",
        }
    )
    harness = make_harness(project, alloy_home, bead=bead)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["complexity"] == "complex"
    assert final["complexity_source"] == "override"

    calls = harness.store.agent_calls(harness.run_id)
    assert [c["role"] for c in calls if c["role"] == "estimate"] == []


async def test_missing_estimate_runner_defaults_medium_and_run_completes(
    project, alloy_home, fake_harnesses
):
    config = load_config()
    roles = dict(config.roles)
    roles["estimate"] = RoleSpec(runner="missing-estimate-runner", fallback=None)
    config = replace(config, roles=roles)

    fake_harnesses.configure(script())
    harness = make_harness(project, alloy_home, config=config)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["complexity"] == "medium"
    assert final["complexity_source"] == "default"


# -- alloy:calibration aggregation and estimate prompt (alloy-4ef.12) ----------


_ESTIMATE_BRIEF = "# alloy-fixture: Add slugify()\n\nAdd a slugify() helper to mypkg."
_ESTIMATE_ACCEPTANCE = "slugify('Hello World') == 'hello-world'"
_ESTIMATE_CONTEXT = {
    "summary": "A small python package with a pytest suite.",
    "relevant_files": ["mypkg/__init__.py"],
    "conventions": ["snake_case"],
    "risks": [],
    "check_hints": ["python -m pytest -q"],
}


def _calibration_imports():
    from alloy.models import CALIBRATION_KEY, format_calibration, update_calibration

    return CALIBRATION_KEY, format_calibration, update_calibration


def _medium_calibration_body() -> str:
    _, _, update_calibration = _calibration_imports()
    body = update_calibration("", level="medium", iterations=3, agent_calls=7, overrun=False)
    return update_calibration(body, level="medium", iterations=3, agent_calls=7, overrun=False)


def test_update_calibration_twice_accumulates_runs_and_mean_iterations():
    """Two medium finishes yield runs=2 and mean_iterations at one decimal."""
    body = _medium_calibration_body()
    medium = json.loads(body)["medium"]
    assert medium["runs"] == 2
    assert medium["mean_iterations"] == 3.0
    assert f"{medium['mean_iterations']:.1f}" == "3.0"


def test_format_calibration_renders_deterministic_single_line():
    """Stored JSON renders as one calibration: line with fixed numeric formatting."""
    _, format_calibration, _ = _calibration_imports()
    body = _medium_calibration_body()
    line = format_calibration(body)
    assert line.startswith("calibration:")
    assert line.count("calibration:") == 1
    assert line == "calibration: medium 2 runs avg 3.0 it avg 7.0 calls 0 overruns"


def test_estimate_prompt_includes_calibration_line_in_project_layer():
    """Estimate alone sees calibration in the project layer, not via memory_block."""
    calibration_key, format_calibration, _ = _calibration_imports()
    body = _medium_calibration_body()
    line = format_calibration(body)
    memory_block = ProjectMemory.from_raw({"human:note": "use pathlib"}, MemorySpec()).render()

    prompt = estimate_prompt(
        _ESTIMATE_BRIEF,
        _ESTIMATE_ACCEPTANCE,
        _ESTIMATE_CONTEXT,
        memory=memory_block,
        calibration=line,
    )
    project_layer = prompt.split(LAYER_SEPARATOR)[1]

    assert project_layer.count("calibration:") == 1
    assert line in project_layer
    assert calibration_key not in project_layer
    assert "Bead design notes outrank project memory." in project_layer
