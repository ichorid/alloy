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
    return load_config()


def shadow_config():
    config = load_config()
    return replace(config, complexity=replace(config.complexity, routing="shadow"))


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


async def test_live_simple_tier_dispatches_cursor_and_records_dispatch_tier(project, alloy_home, fake_harnesses):
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


async def test_live_complex_tier_dispatches_cursor_with_kimi_model(project, alloy_home, fake_harnesses):
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


async def test_live_simple_tier_fails_over_to_codex_when_cursor_missing(project, alloy_home, fake_harnesses):
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
    assert rows[1]["model"] == "gpt-6-luna"
    assert rows[1]["ok"]


async def test_live_medium_tier_fails_over_to_codex_terra_when_cursor_missing(project, alloy_home, fake_harnesses):
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
    assert second["model"] == "gpt-6-sol"
    assert second["ok"]


async def test_shadow_routing_keeps_astra_implement_and_no_dispatch_tier(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(workflow_script())
    harness = make_harness(
        project,
        alloy_home,
        config=shadow_config(),
        bead=bead_with_complexity("simple"),
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    row = _first_role_row(harness, "implement")
    # Shadow mode: implement still runs the recipe's `astra` role,
    # and the ledger records astra's resolved runner, as in test_estimate.py.
    assert row["runner"] == "codex"

    run = harness.store.get_run(harness.run_id)
    assert run is not None
    assert run.get("dispatch_tier") is None


async def test_live_routing_leaves_non_tiered_roles_on_recipe_specs(project, alloy_home, fake_harnesses):
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


# -- judge-retry escalation (alloy-0uc.12) ----------------------------------


def retry_escalation_script(**overrides):
    """Three implement rounds with judge retry, retry, then done."""
    base = workflow_script(
        implement=[
            implement_entry(succeed=True),
            implement_entry(succeed=True),
            implement_entry(succeed=True),
        ],
        judge=[
            judge_entry("retry", "needs another pass"),
            judge_entry("retry", "still not satisfied"),
            judge_entry("done"),
        ],
    )
    base.update(overrides)
    return base


def _ok_implement_rows(harness, *, iteration: int | None = None) -> list[dict]:
    rows = [r for r in _implement_ledger_rows(harness) if r["ok"]]
    if iteration is not None:
        rows = [r for r in rows if r["iteration"] == iteration]
    return rows


def test_runs_table_has_escalations_counter_column(alloy_home):
    store = Store(alloy_home / "alloy.db")
    with store.connect() as conn:
        info = {row["name"]: row for row in conn.execute("PRAGMA table_info(runs)")}
    assert "escalations" in info
    assert info["escalations"]["type"].upper() == "INTEGER"
    assert info["escalations"]["dflt_value"] == "0"


async def test_live_escalates_simple_to_medium_after_two_consecutive_judge_retries(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(retry_escalation_script())
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
    assert final["iteration"] == 3

    for iteration in (1, 2, 3):
        rows = _implement_ledger_rows(harness, iteration=iteration)
        assert len(rows) == 1
        assert rows[0]["runner"] == "cursor"
        assert rows[0]["model"] == "composer-2.5"

    assert final["complexity"] == "medium"
    assert len(final["escalations"]) == 1
    entry = final["escalations"][0]
    assert entry["from"] == "simple"
    assert entry["to"] == "medium"
    assert entry["iteration"] == 2

    run = harness.store.get_run(harness.run_id)
    assert run is not None
    assert run["escalations"] == 1
    assert run["dispatch_tier"] == "medium"


async def test_live_escalation_changes_codex_fallback_when_cursor_missing(project, alloy_home, fake_harnesses):
    fake_harnesses.remove("cursor-agent")
    fake_harnesses.configure(retry_escalation_script())
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

    row1 = _ok_implement_rows(harness, iteration=1)[0]
    assert row1["runner"] == "codex"
    assert row1["model"] == "gpt-6-luna"

    row2 = _ok_implement_rows(harness, iteration=2)[0]
    assert row2["runner"] == "codex"
    assert row2["model"] == "gpt-6-luna"

    row3 = _ok_implement_rows(harness, iteration=3)[0]
    assert row3["runner"] == "codex"
    assert row3["model"] == "gpt-6-sol"


async def test_live_escalates_medium_to_complex_after_two_consecutive_judge_retries(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(retry_escalation_script())
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
    row3 = _implement_ledger_rows(harness, iteration=3)[0]
    assert row3["runner"] == "cursor"
    assert row3["model"] == "kimi-k3-high"
    assert final["complexity"] == "complex"


async def test_shadow_routing_never_escalates_on_judge_retries(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(retry_escalation_script())
    harness = make_harness(
        project,
        alloy_home,
        config=shadow_config(),
        bead=bead_with_complexity("simple"),
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    for iteration in (1, 2, 3):
        rows = _implement_ledger_rows(harness, iteration=iteration)
        assert len(rows) == 1
        assert harness.recipe_config.role("implement").runner == "astra"
        assert rows[0]["runner"] == "codex"  # astra's resolved runner is recorded

    assert final["escalations"] == []
    run = harness.store.get_run(harness.run_id)
    assert run is not None
    assert run["escalations"] == 0


async def test_live_complex_tier_does_not_escalate_further_on_judge_retries(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(retry_escalation_script())
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
    assert final["complexity"] == "complex"
    assert final["escalations"] == []

    run = harness.store.get_run(harness.run_id)
    assert run is not None
    assert run["escalations"] == 0


async def test_consilium_between_retries_resets_escalation_counter(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        retry_escalation_script(
            implement=[
                implement_entry(succeed=True),
                implement_entry(succeed=True),
                implement_entry(succeed=True),
                implement_entry(succeed=True),
            ],
            judge=[
                judge_entry("retry", "first retry"),
                judge_entry("consilium", "stuck"),
                judge_entry("retry", "after consilium"),
                judge_entry("done"),
            ],
        )
    )
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
    assert final["complexity"] == "simple"
    assert final["escalations"] == []

    run = harness.store.get_run(harness.run_id)
    assert run is not None
    assert run["escalations"] == 0
    assert run["dispatch_tier"] == "simple"
