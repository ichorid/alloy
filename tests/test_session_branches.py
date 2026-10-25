"""The tests writer's session is branched for the implementer and the verifier.

The agent that wrote (or repaired) the tests writes the code, in its own fork
of the tests session (warm prompt cache, task and tests in context), and keeps
that fork across iterations. The verifier is a second, read-only fork of the
same tests session, so it shares the tests writer's context without seeing the
implementer's turns. While writing tests the agent is never told it will
implement. `sessions.branch_from_tests: false` restores cold calls.
"""

from __future__ import annotations

from dataclasses import replace

from conftest import implement_entry, judge_entry, verifier_run_entry, verifier_stop_entry, write_tests_entry
from support import load_config, make_harness
from workflow_support import FULL_SUITE, script

from alloy.config import RoleSpec
from alloy.recipes.role_prompts import IMPLEMENT_RESUMED

REPOSITORY_CONTEXT_HEADING = "## Repository context"


def _codex_config(**changes):
    config = load_config()
    roles = dict(config.roles)
    roles["tests"] = RoleSpec(runner="codex", model="gpt-6.1-sol")
    roles["implement"] = RoleSpec(
        runner="codex", model="gpt-6.1-sol", fallback=RoleSpec(runner="claude-write", model="sonnet")
    )
    roles["verifier"] = RoleSpec(runner="cursor", model="composer-2.5")
    return replace(config, roles=roles, **changes)


def _scenario(**overrides):
    verify = [verifier_run_entry(FULL_SUITE, kind="regression"), verifier_stop_entry("green")] * 3
    base = script(
        **{
            "tests@codex": {**write_tests_entry(), "session_id": "tests-thread"},
            "implement@codex": [
                {**implement_entry(succeed=True), "session_id": "impl-thread"},
                {**implement_entry(succeed=True), "session_id": "impl-thread"},
            ],
            "verifier@codex": [{**entry, "session_id": "verifier-thread"} for entry in verify],
            "judge": [judge_entry("retry", instructions="Tighten the edge case."), judge_entry("done")],
        }
    )
    base.pop("tests", None)
    base.pop("implement", None)
    base.update(overrides)
    return base


async def _run(project, alloy_home, config):
    harness = make_harness(project, alloy_home, config=config)
    try:
        return await harness.start()
    finally:
        harness.close()


async def test_implementer_and_verifier_branch_the_tests_session(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(_scenario())

    final = await _run(project, alloy_home, _codex_config())

    assert final["outcome"] == "done"
    tests_call = fake_harnesses.calls_for("tests")[0]
    assert "Do not implement" in tests_call["prompt"]
    assert "you will implement" not in tests_call["prompt"].lower()

    implement = fake_harnesses.calls_for("implement")
    assert len(implement) >= 2
    assert implement[0]["runner"] == "codex" and implement[0].get("fork") == "tests-thread"
    assert IMPLEMENT_RESUMED in implement[0]["prompt"]
    assert REPOSITORY_CONTEXT_HEADING not in implement[0]["prompt"]
    assert all(call.get("resume") == "impl-thread" for call in implement[1:])

    verifier = fake_harnesses.calls_for("verifier")
    assert verifier[0]["runner"] == "codex" and verifier[0].get("fork") == "tests-thread"
    assert 'sandbox_mode="read-only"' in verifier[0]["argv"]
    assert all(call.get("resume") == "verifier-thread" for call in verifier[1:])

    assert final["implement_session"]["forked_from_revision"] == final["tests_session"]["revision"] == 1


async def test_branching_off_runs_cold_as_before(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(_scenario())

    final = await _run(project, alloy_home, _codex_config(branch_from_tests=False))

    assert final["outcome"] == "done"
    assert not [call for call in fake_harnesses.calls if call.get("fork")]
    assert not [call for call in fake_harnesses.calls_for("implement") if call.get("resume")]


async def test_a_branch_that_fails_runs_cold_and_is_continued_later(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        _scenario(
            **{
                "implement@codex": [
                    {"exit": 1, "stderr": "no such session", "session_id": "tests-thread"},
                    {**implement_entry(succeed=True), "session_id": "cold-thread"},
                    {**implement_entry(succeed=True), "session_id": "cold-thread"},
                ]
            }
        )
    )

    final = await _run(project, alloy_home, _codex_config())

    assert final["outcome"] == "done"
    implement = fake_harnesses.calls_for("implement")
    assert implement[0].get("fork") == "tests-thread"
    assert "fork" not in implement[1] and "resume" not in implement[1]
    assert REPOSITORY_CONTEXT_HEADING in implement[1]["prompt"]
    assert implement[2].get("resume") == "cold-thread"
