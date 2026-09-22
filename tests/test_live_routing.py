"""Live complexity routing: tier chains dispatch tiered roles (acceptance for alloy-0uc.3)."""

from __future__ import annotations

from dataclasses import replace

from alloy.beads import Bead
from conftest import (
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    write_tests_entry,
)
from alloy.store import Store
from support import load_config, make_bead, make_harness


def workflow_script(**overrides):
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


def live_config():
    config = load_config()
    return replace(config, complexity=replace(config.complexity, routing="live"))


def bead_with_complexity(level: str) -> Bead:
    return make_bead(
        metadata={
            "alloy_recipe": "tdd-loop",
            "alloy_complexity": level,
        }
    )


def _implement_ledger_rows(harness, *, iteration: int | None = None) -> list[dict]:
    rows = [c for c in harness.store.agent_calls(harness.run_id) if c["role"] == "implement"]
    if iteration is not None:
        rows = [c for c in rows if c["iteration"] == iteration]
    return rows


def _first_role_row(harness, role: str) -> dict:
    rows = [c for c in harness.store.agent_calls(harness.run_id) if c["role"] == role]
    assert rows, f"expected at least one ledger row for role {role!r}"
    return rows[0]


def test_runs_table_has_nullable_dispatch_tier_column(alloy_home):
    store = Store(alloy_home / "alloy.db")
    with store.connect() as conn:
        info = {row["name"]: row for row in conn.execute("PRAGMA table_info(runs)")}
    assert "dispatch_tier" in info
    assert info["dispatch_tier"]["notnull"] == 0


async def test_live_simple_tier_dispatches_cursor_and_records_dispatch_tier(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(workflow_script())
    harness = make_harness(
        project,
        alloy_home,
        config=live_config(),
        bead=bead_with_complexity("simple"),
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    row = _first_role_row(harness, "implement")
    assert row["runner"] == "cursor"
    assert row["model"] == "composer-2.5"

    run = harness.store.get_run(harness.run_id)
    assert run is not None
    assert run["dispatch_tier"] == "simple"


async def test_live_complex_tier_dispatches_cursor_with_kimi_model(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(workflow_script())
    harness = make_harness(
        project,
        alloy_home,
        config=live_config(),
        bead=bead_with_complexity("complex"),
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    row = _first_role_row(harness, "implement")
    assert row["runner"] == "cursor"
    assert row["model"] == "kimi-k3-high"


async def test_live_simple_tier_fails_over_to_codex_when_cursor_missing(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.remove("cursor-agent")
    fake_harnesses.configure(workflow_script())
    harness = make_harness(
        project,
        alloy_home,
        config=live_config(),
        bead=bead_with_complexity("simple"),
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["implementer"] == "codex"

    rows = _implement_ledger_rows(harness, iteration=1)
    assert len(rows) == 2
    assert rows[0]["runner"] == "cursor"
    assert not rows[0]["ok"]
    assert rows[0]["exit_code"] == 127
    assert rows[1]["runner"] == "codex"
    assert rows[1]["model"] == "gpt-5.6-luna"
    assert rows[1]["ok"]


async def test_live_medium_tier_fails_over_to_codex_terra_when_cursor_missing(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.remove("cursor-agent")
    fake_harnesses.configure(workflow_script())
    harness = make_harness(
        project,
        alloy_home,
        config=live_config(),
        bead=bead_with_complexity("medium"),
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    rows = _implement_ledger_rows(harness, iteration=1)
    assert len(rows) >= 2
    second = rows[1]
    assert second["runner"] == "codex"
    assert second["model"] == "gpt-5.6-terra"
    assert second["ok"]


async def test_shadow_routing_keeps_astra_implement_and_no_dispatch_tier(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(workflow_script())
    harness = make_harness(
        project,
        alloy_home,
        config=load_config(),
        bead=bead_with_complexity("simple"),
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    row = _first_role_row(harness, "implement")
    # Shadow mode is unchanged: implement still runs the recipe's `astra` role,
    # and the ledger records astra's resolved runner, as in test_estimate.py.
    assert row["runner"] == "codex"

    run = harness.store.get_run(harness.run_id)
    assert run is not None
    assert run.get("dispatch_tier") is None


async def test_live_routing_leaves_non_tiered_roles_on_recipe_specs(
    project, alloy_home, fake_harnesses
):
    config = live_config()
    fake_harnesses.configure(workflow_script())
    harness = make_harness(
        project,
        alloy_home,
        config=config,
        bead=bead_with_complexity("simple"),
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"

    context_row = _first_role_row(harness, "context")
    assert context_row["runner"] == "cursor-plan"

    tests_row = _first_role_row(harness, "tests")
    assert tests_row["runner"] == "cursor"
    assert tests_row["model"] == "composer-2.5"

    judge_row = _first_role_row(harness, "judge")
    assert judge_row["runner"] == config.role("judge").runner
