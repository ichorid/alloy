"""Estimate step: complexity classification after context, before tests."""

from __future__ import annotations

from dataclasses import replace

from alloy.config import RoleSpec
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
    assert harness.recipe_config.role("implement").runner == "astra"
    assert implement_row["runner"] == "codex"  # astra's resolved runner is recorded


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
