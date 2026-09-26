"""tests_review: an independent read-only reviewer checks the red tests before implement."""

from __future__ import annotations

from dataclasses import replace

from conftest import (
    context_entry,
    implement_entry,
    judge_entry,
    write_tests_entry,
)
from support import load_config, make_bead, make_harness

from alloy.config import RoleSpec


def review_entry(verdict: str, *issues: str) -> dict:
    return {
        "structured": {
            "verdict": verdict,
            "issues": list(issues),
            "reason": f"scripted {verdict}",
            "confidence": 0.9,
        }
    }


def config_with_review(*, max_reviews: int = 2, tiered_tests: bool = False):
    config = load_config()
    roles = dict(config.roles)
    roles["tests_review"] = RoleSpec(runner="codex-readonly", timeout_minutes=10)
    if tiered_tests:
        roles["tests"] = replace(roles["tests"], tiered=True)
    return replace(
        config,
        roles=roles,
        verification=replace(config.verification, max_test_reviews=max_reviews),
    )


def script(**overrides):
    base = {
        "context": context_entry(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "judge": [judge_entry("done")],
    }
    base.update(overrides)
    return base


def rows(harness, role: str) -> list[dict]:
    return [c for c in harness.store.agent_calls(harness.run_id) if c["role"] == role]


async def run(project, alloy_home, config, **bead_meta):
    harness = make_harness(
        project,
        alloy_home,
        config=config,
        bead=make_bead(metadata={"alloy_recipe": "tdd-loop", **bead_meta}),
    )
    try:
        final = await harness.start()
    finally:
        harness.close()
    return harness, final


async def test_revise_sends_tests_back_then_a_sound_review_lets_implement_start(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        script(
            tests_review=[
                review_entry("revise", "no test covers the empty-string criterion"),
                review_entry("sound"),
            ]
        )
    )
    harness, final = await run(project, alloy_home, config_with_review())

    assert final["outcome"] == "done"
    assert len(rows(harness, "tests")) == 2
    assert len(rows(harness, "tests_review")) == 2
    assert rows(harness, "tests_review")[0]["runner"] == "codex-readonly"


async def test_review_rounds_are_bounded_by_max_test_reviews(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script(tests_review=review_entry("revise", "still weak")))
    harness, final = await run(project, alloy_home, config_with_review(max_reviews=1))

    assert final["outcome"] == "done"
    assert len(rows(harness, "tests_review")) == 1
    assert len(rows(harness, "tests")) == 2  # one revision, then implement regardless


async def test_a_failing_reviewer_never_blocks_the_run(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script(tests_review={"structured": {"nonsense": True}}))
    harness, final = await run(project, alloy_home, config_with_review())

    assert final["outcome"] == "done"
    assert len(rows(harness, "tests")) == 1


async def test_recipes_without_the_role_do_not_review(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script())
    harness, final = await run(project, alloy_home, load_config())

    assert final["outcome"] == "done"
    assert rows(harness, "tests_review") == []


async def test_tiered_tests_role_uses_the_complexity_tier_chain(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(script())
    harness, final = await run(
        project,
        alloy_home,
        config_with_review(tiered_tests=True),
        alloy_complexity="complex",
    )

    assert final["outcome"] == "done"
    row = rows(harness, "tests")[0]
    assert (row["runner"], row["model"]) == ("cursor", "kimi-k3-high")


def config_with_panel(*, fallback: bool = True):
    config = config_with_review()
    roles = dict(config.roles)
    roles["tests_review"] = RoleSpec(
        runner="claude",
        panel=(
            RoleSpec(runner="cursor-plan", model="composer-2.5"),
            RoleSpec(runner="codex-readonly", model="gpt-6-luna"),
        ),
        fallback=RoleSpec(runner="claude", model="sonnet", effort="low") if fallback else None,
    )
    return replace(config, roles=roles)


async def test_panel_merges_issues_from_every_member(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        script(
            tests_review=[
                review_entry("revise", "issue from one member"),
                review_entry("revise", "issue from the other", "issue from one member"),
                review_entry("sound"),
                review_entry("sound"),
            ]
        )
    )
    harness, final = await run(project, alloy_home, config_with_panel(fallback=False))

    assert final["outcome"] == "done"
    runners = {c["runner"] for c in rows(harness, "tests_review")}
    assert {"cursor-plan", "codex-readonly"} <= runners
    assert len(rows(harness, "tests_review")) >= 2
    assert len(rows(harness, "tests")) >= 2


async def test_panel_runs_on_the_remaining_member_when_one_is_missing(project, alloy_home, fake_harnesses):
    fake_harnesses.remove("cursor-agent")
    fake_harnesses.configure(
        script(
            tests_review=[
                review_entry("revise", "only the codex member is left"),
                review_entry("sound"),
            ]
        )
    )
    harness, final = await run(project, alloy_home, config_with_panel())

    assert final["outcome"] == "done"
    reviews = rows(harness, "tests_review")
    assert [c["runner"] for c in reviews] == ["codex-readonly", "codex-readonly"]
    assert all(c["model"] == "gpt-6-luna" for c in reviews)
    assert len([c for c in rows(harness, "tests") if c["ok"]]) == 2


async def test_panel_falls_back_to_sonnet_low_when_every_member_is_missing(project, alloy_home, fake_harnesses):
    fake_harnesses.remove("cursor-agent")
    fake_harnesses.remove("codex")
    fake_harnesses.configure(
        script(
            tests_review=[
                review_entry("revise", "fallback reviewer speaking"),
                review_entry("sound"),
            ]
        )
    )
    harness, final = await run(project, alloy_home, config_with_panel())

    assert final["outcome"] == "done"
    reviews = rows(harness, "tests_review")
    assert [(c["runner"], c["model"]) for c in reviews] == [("claude", "sonnet")] * 2
    assert len([c for c in rows(harness, "tests") if c["ok"]]) == 2
