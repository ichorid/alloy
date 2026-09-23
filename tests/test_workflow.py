"""Workflow routing, limits, consilium and the human gate.

Every test scripts the agents' decisions and then asserts on what Alloy did with
them -- especially where Alloy overrules the agent.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from alloy.beads import BeadsClient
from alloy.config import Limits, RoleSpec, VerificationSpec
from alloy.models import parse_provenance
from alloy.store import Store
from alloy.recipes.tdd_loop import tests_prompt, verifier_prompt
from conftest import (
    FAKE_BD_SOURCE,
    FAKE_RUNNERS,
    FAKE_SOURCE,
    acceptance_entry,
    context_entry,
    critic_entry,
    implement_empty_diff_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    verifier_run_entry,
    verifier_stop_entry,
    write_tests_entry,
)
from support import load_config, make_bead, make_harness


def script(**overrides):
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


# -- the happy path ---------------------------------------------------------


async def test_done_decision_finishes_after_one_iteration(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(script())
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["iteration"] == 1
    assert [call["role"] for call in fake_harnesses.calls] == [
        "context", "estimate", "tests", "implement", "verifier", "acceptance", "judge"
    ]


async def test_stages_run_in_order_and_tests_precede_implementation(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(script())
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    roles = [call["role"] for call in fake_harnesses.calls]
    assert roles.index("tests") < roles.index("implement")


async def test_baseline_records_that_the_new_tests_fail_first(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(verification_script())
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["baseline"][0]["exit_code"] != 0  # tests were failing before implementation
    assert final["last_check"]["exit_code"] == 0  # and passing after


async def test_implement_change_summary_extracts_marked_summary_block(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(
        script(
            implement=[
                {
                    **implement_entry(succeed=True),
                    "text": (
                        "noise <summary>Implemented slugify.</summary> trailing chatter"
                    ),
                }
            ],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["attempts"][0]["change_summary"] == "Implemented slugify."


# -- targeted baseline (prove_red) ------------------------------------------


async def test_prove_red_runs_targeted_baseline_checks_from_tests_role(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(script())
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    baseline = final["baseline"]
    assert isinstance(baseline, list)
    assert len(baseline) == 1
    command = baseline[0]["command"]
    assert command.endswith("tests/test_slugify.py")
    assert baseline[0]["exit_code"] != 0

    log_dir = alloy_home / "logs" / harness.run_id
    targeted_logs = list(log_dir.glob("check-*-targeted-*.log"))
    assert len(targeted_logs) == 1
    assert targeted_logs[0].read_text(encoding="utf-8").splitlines()[0] == f"$ {command}"


async def test_implement_prompt_lists_baseline_checks_not_whole_suite_command(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(script())
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    first_implement = fake_harnesses.calls_for("implement")[0]["prompt"]
    assert "Checks that must go green" in first_implement
    assert f"{sys.executable} -m pytest -q tests/test_slugify.py" in first_implement
    assert "## Test command" not in first_implement


async def test_green_baseline_reruns_tests_then_proceeds_when_red(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(
        script(tests=[write_tests_entry(passing=True), write_tests_entry()])
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["baseline_repairs"] == 1
    tests_calls = fake_harnesses.calls_for("tests")
    assert len(tests_calls) == 2
    assert "passed" in tests_calls[1]["prompt"]
    assert f"{sys.executable} -m pytest -q tests/test_slugify.py" in tests_calls[1]["prompt"]


async def test_always_green_baseline_parks_at_human_gate(
    project, alloy_home, fake_harnesses
):
    config = replace(
        load_config(),
        verification=replace(load_config().verification, max_baseline_repairs=1),
    )
    fake_harnesses.configure(
        script(tests=[write_tests_entry(passing=True), write_tests_entry(passing=True)])
    )
    harness = make_harness(project, alloy_home, config=config)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert "__interrupt__" in final
    reason = final["__interrupt__"][0].value["reason"]
    assert reason.startswith("baseline unexpectedly green")
    assert len(fake_harnesses.calls_for("tests")) == 2
    assert fake_harnesses.calls_for("implement") == []


async def test_unrunnable_baseline_reruns_tests_with_command_not_found(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(
        script(
            tests=[
                write_tests_entry(
                    baseline_checks=[
                        {
                            "command": "definitely-not-a-program",
                            "purpose": "probe missing executable",
                        }
                    ]
                ),
                write_tests_entry(),
            ]
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    tests_calls = fake_harnesses.calls_for("tests")
    assert len(tests_calls) == 2
    assert "command not found" in tests_calls[1]["prompt"]
    assert "definitely-not-a-program" in tests_calls[1]["prompt"]


# -- retry ------------------------------------------------------------------


async def test_retry_loops_back_through_implementation_and_verification(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[
                judge_entry("retry", "slugify returns None", "return the slug string"),
                judge_entry("done"),
            ],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["iteration"] == 2
    assert len(fake_harnesses.calls_for("implement")) == 2
    assert len(final["attempts"]) == 2


async def test_retry_instructions_reach_the_next_implementation(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[judge_entry("retry", "wrong return", "return the slug string"),
                   judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    second = fake_harnesses.calls_for("implement")[1]["prompt"]
    assert "return the slug string" in second
    assert "Previous attempts" in second


async def test_history_given_to_agents_is_compact_not_a_transcript(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(
        script(
            implement=[
                {**implement_entry(succeed=False), "text": "X" * 50_000},
                implement_entry(succeed=True),
            ],
            judge=[judge_entry("retry", "again"), judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert len(final["attempts"][0]["change_summary"]) < 1000
    assert len(fake_harnesses.calls_for("implement")[1]["prompt"]) < 30_000


# -- Alloy overruling the judge --------------------------------------------


async def test_done_is_refused_while_tests_are_failing(
    project, alloy_home, fake_harnesses
):
    """The judge does not get to declare victory over a red suite."""
    fake_harnesses.configure(
        verification_script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[judge_entry("done", "looks right to me"), judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["iteration"] == 2  # the premature "done" was turned into a retry
    assert "overridden by Alloy" in final["attempts"][0]["reason"] or \
           len(fake_harnesses.calls_for("implement")) == 2


async def test_unparseable_judge_output_becomes_a_retry_not_a_crash(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=True)],
            judge=[{"text": "I think it's fine, honestly"}, judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["iteration"] == 2


async def test_judge_runner_failure_is_survived(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=True)],
            judge=[{"exit": 1, "stderr": "claude: overloaded"}, judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert "judge runner failed" in final["attempts"][0]["reason"]


async def test_failing_tests_role_pauses_for_a_human_before_burning_an_implementation(
    project, alloy_home, fake_harnesses
):
    """A session limit on the tests harness is not an impossible task: park,
    don't fail, and re-run the tests stage on resume."""
    from langgraph.types import Command

    fake_harnesses.configure(script(tests=[{"exit": 1, "stderr": "claude: session limit"},
                                           write_tests_entry()]))
    harness = make_harness(project, alloy_home)
    try:
        paused = await harness.start()
        assert "__interrupt__" in paused
        assert "the tests role failed" in paused["__interrupt__"][0].value["reason"]
        assert fake_harnesses.calls_for("implement") == []

        final = await harness.resume(Command(resume={"instructions": "limit reset, go"}))
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert [call["role"] for call in fake_harnesses.calls] == [
        "context", "estimate", "tests", "tests", "tests", "implement", "verifier",
        "acceptance", "judge",
    ]
    # Cursor and its fallback fail before the gate; resume retries Cursor.
    assert [call["runner"] for call in fake_harnesses.calls_for("tests")] == [
        "cursor-agent", "claude", "cursor-agent"
    ]


# -- consilium --------------------------------------------------------------


async def test_consilium_runs_every_critic_then_synthesizes(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[judge_entry("consilium", "stuck"), judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    critics = fake_harnesses.calls_for("critic")
    assert {call["runner"] for call in critics} == {"claude", "codex", "pi"}
    assert len(fake_harnesses.calls_for("synthesize")) == 1
    assert final["consiliums"] == 1
    assert final["outcome"] == "done"


async def test_critics_never_see_each_others_opinions(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[judge_entry("consilium", "stuck"), judge_entry("done")],
            critic=critic_entry("a very distinctive root cause"),
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    for call in fake_harnesses.calls_for("critic"):
        assert "a very distinctive root cause" not in call["prompt"]
    # ...but the synthesizer sees all of them
    assert fake_harnesses.calls_for("synthesize")[0]["prompt"].count(
        "a very distinctive root cause"
    ) == 3


async def test_critics_are_read_only_harnesses(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[judge_entry("consilium", "stuck"), judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    codex_critic = next(
        call for call in fake_harnesses.calls_for("critic") if call["runner"] == "codex"
    )
    assert "read-only" in codex_critic["argv"]


async def test_synthesized_instructions_drive_the_next_implementation(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[judge_entry("consilium", "stuck"), judge_entry("done")],
            synthesize=synthesize_entry("Return the slug, not None."),
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    assert "Return the slug, not None." in fake_harnesses.calls_for("implement")[1]["prompt"]


async def test_missing_critic_harness_narrows_the_consilium_instead_of_failing(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.remove("pi")
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[judge_entry("consilium", "stuck"), judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert {call["runner"] for call in fake_harnesses.calls_for("critic")} == {"claude", "codex"}
    assert final["outcome"] == "done"


async def test_a_failing_critic_does_not_sink_the_consilium(
    project, alloy_home, fake_harnesses
):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[judge_entry("consilium", "stuck"), judge_entry("done")],
            **{"critic@pi": {"exit": 1, "stderr": "pi: unavailable"}},
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert len(fake_harnesses.calls_for("synthesize")) == 1


# -- limits -----------------------------------------------------------------


async def test_max_iterations_stops_an_endless_retry_loop(
    project, alloy_home, fake_harnesses
):
    """The judge asks for retry forever; Alloy stops anyway."""
    config = replace(load_config(), limits=Limits(max_iterations=3, max_consiliums=0,
                                                  max_agent_calls=100))
    fake_harnesses.configure(
        script(implement=[implement_entry(succeed=False)],
               judge=[judge_entry("retry", "still broken")])
    )
    harness = make_harness(project, alloy_home, config=config)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] is None  # paused, not finished
    assert "__interrupt__" in final
    assert final["iteration"] == 3
    assert "max_iterations" in final["limit_hit"]


async def test_max_agent_calls_stops_the_loop(project, alloy_home, fake_harnesses):
    config = replace(load_config(), limits=Limits(max_iterations=50, max_consiliums=0,
                                                  max_agent_calls=6))
    fake_harnesses.configure(
        script(implement=[implement_entry(succeed=False)],
               judge=[judge_entry("retry", "still broken")])
    )
    harness = make_harness(project, alloy_home, config=config)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert "max_agent_calls" in final["limit_hit"]


async def test_max_wall_time_stops_the_loop(project, alloy_home, fake_harnesses):
    config = replace(load_config(), limits=Limits(max_iterations=50, max_consiliums=0,
                                                  max_agent_calls=100,
                                                  max_wall_time_minutes=0.0))
    fake_harnesses.configure(
        script(implement=[implement_entry(succeed=False)],
               judge=[judge_entry("retry", "still broken")])
    )
    harness = make_harness(project, alloy_home, config=config)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert "max_wall_time" in final["limit_hit"]


async def test_consilium_budget_downgrades_to_a_plain_retry(
    project, alloy_home, fake_harnesses
):
    config = replace(load_config(), limits=Limits(max_iterations=5, max_consiliums=1,
                                                  max_agent_calls=100))
    fake_harnesses.configure(
        script(
            implement=[
                implement_entry(succeed=False),
                implement_entry(succeed=False),
                implement_entry(succeed=True),
            ],
            judge=[
                judge_entry("consilium", "stuck"),
                judge_entry("consilium", "still stuck"),
                judge_entry("done"),
            ],
        )
    )
    harness = make_harness(project, alloy_home, config=config)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["consiliums"] == 1  # the second request was refused
    assert len(fake_harnesses.calls_for("synthesize")) == 1
    assert final["outcome"] == "done"


async def test_abort_fails_the_task_cleanly(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        script(implement=[implement_entry(succeed=False)],
               judge=[judge_entry("abort", "the spec contradicts itself")])
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "failed"
    assert final["outcome_reason"] == "the spec contradicts itself"


# -- the human gate ---------------------------------------------------------


async def test_human_decision_pauses_at_a_checkpoint(project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        script(implement=[implement_entry(succeed=False)],
               judge=[judge_entry("human", "which unicode form?", "ask the author")])
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
        interrupts = final["__interrupt__"]
        assert interrupts
        payload = interrupts[0].value
        assert payload["reason"] == "which unicode form?"
        assert payload["bead_id"] == "t-1"
        assert final.get("outcome") is None
    finally:
        harness.close()


async def test_resume_delivers_the_humans_answer_to_the_implementer(
    project, alloy_home, fake_harnesses
):
    from langgraph.types import Command

    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[judge_entry("human", "which unicode form?"), judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
        final = await harness.resume(Command(resume={"instructions": "use NFKD"}))
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert "use NFKD" in fake_harnesses.calls_for("implement")[1]["prompt"]


async def test_resuming_past_a_limit_grants_a_fresh_budget(
    project, alloy_home, fake_harnesses
):
    """Otherwise the run would pause again immediately and never progress."""
    from langgraph.types import Command

    config = replace(load_config(), limits=Limits(max_iterations=2, max_consiliums=0,
                                                  max_agent_calls=100))
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=False),
                       implement_entry(succeed=True)],
            judge=[judge_entry("retry", "broken"), judge_entry("retry", "broken"),
                   judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home, config=config)
    try:
        first = await harness.start()
        assert "max_iterations" in first["limit_hit"]
        final = await harness.resume(Command(resume={"instructions": "keep going"}))
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert final["budget_extensions"] == 1


async def test_a_judge_that_always_says_done_on_a_red_suite_still_hits_the_limit(
    project, alloy_home, fake_harnesses
):
    """The premature-done override must not become a way around max_iterations."""
    config = replace(load_config(), limits=Limits(max_iterations=3, max_consiliums=0,
                                                  max_agent_calls=100))
    fake_harnesses.configure(
        verification_script(
            implement=[implement_entry(succeed=False)],
            judge=[judge_entry("done")],
            # Always asks for the suite; every red result goes straight to repair.
            verifier=verifier_run_entry(FULL_SUITE, kind="regression"),
        )
    )
    harness = make_harness(project, alloy_home, config=config)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["iteration"] == 3
    assert "max_iterations" in final["limit_hit"]
    assert "__interrupt__" in final


# -- verification_loop (alloy-21u.4) ----------------------------------------


TARGETED_SLUGIFY = f"{sys.executable} -m pytest -q tests/test_slugify.py"
FULL_SUITE = f"{sys.executable} -m pytest -q"


def verification_script(**overrides):
    """Happy-path verifier scripting for the dynamic verification loop."""
    base = script(
        verifier=[
            verifier_run_entry(FULL_SUITE, kind="regression"),
            verifier_stop_entry("regression suite green"),
        ],
    )
    base.update(overrides)
    return base


async def test_red_required_check_routes_to_implement_without_judge(
    project, alloy_home, fake_harnesses
):
    """A required check that fails skips the judge and routes straight to repair."""
    fake_harnesses.configure(
        verification_script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            verifier=[
                verifier_run_entry(TARGETED_SLUGIFY, kind="targeted"),
                verifier_stop_entry("targeted check green after repair"),
            ],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    roles = [call["role"] for call in fake_harnesses.calls]
    first_impl = roles.index("implement")
    second_impl = roles.index("implement", first_impl + 1)
    assert roles[first_impl + 1 : second_impl] == ["verifier"]
    assert "judge" not in roles[first_impl + 1 : second_impl]

    second_prompt = fake_harnesses.calls_for("implement")[1]["prompt"]
    assert "Failed check" in second_prompt
    assert "exit 1" in second_prompt
    assert "Current diff" in second_prompt


async def test_verifier_builds_on_green_targeted_before_regression_and_judge(
    project, alloy_home, fake_harnesses
):
    """After a green targeted check the verifier sees it, runs regression, then stops."""
    fake_harnesses.configure(
        verification_script(
            verifier=[
                verifier_run_entry(TARGETED_SLUGIFY, kind="targeted"),
                verifier_run_entry(FULL_SUITE, kind="regression"),
                verifier_stop_entry("targeted and regression both green"),
            ],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    verifier_calls = fake_harnesses.calls_for("verifier")
    assert len(verifier_calls) >= 3
    assert TARGETED_SLUGIFY in verifier_calls[1]["prompt"]
    assert "-> exit 0" in verifier_calls[1]["prompt"] or "passed" in verifier_calls[1]["prompt"].lower()

    roles = [call["role"] for call in fake_harnesses.calls]
    last_verifier = max(i for i, role in enumerate(roles) if role == "verifier")
    judge_idx = roles.index("judge")
    assert judge_idx > last_verifier
    assert "implement" not in roles[last_verifier + 1 : judge_idx]

    kinds = [check["kind"] for check in final["checks"]]
    assert "targeted" in kinds
    assert kinds.index("targeted") < kinds.index("regression")


async def test_verifier_check_kinds_recorded_in_order_across_iterations(
    project, alloy_home, fake_harnesses
):
    """Each iteration's verifier-chosen check kind is stored on the run."""
    fake_harnesses.configure(
        verification_script(
            implement=[implement_entry(succeed=True), implement_entry(succeed=True)],
            verifier=[
                verifier_run_entry(TARGETED_SLUGIFY, kind="targeted"),
                verifier_stop_entry("iteration 1 evidence"),
                verifier_run_entry('sh -c "exit 0"', kind="lint", purpose="lint pass"),
                verifier_stop_entry("iteration 2 evidence"),
            ],
            judge=[
                judge_entry("retry", "one more polish pass"),
                judge_entry("done"),
            ],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    kinds = [check["kind"] for check in final["checks"]]
    assert kinds.index("targeted") < kinds.index("lint")


async def test_max_checks_per_iteration_forces_verifier_stop(
    project, alloy_home, fake_harnesses
):
    """Hard per-iteration check budget stops the verifier and records the exhaustion."""
    config = replace(
        load_config(),
        verification=replace(load_config().verification, max_checks_per_iteration=2),
    )
    fake_harnesses.configure(
        verification_script(
            verifier=[verifier_run_entry('sh -c "exit 0"', kind="custom")] * 10,
        )
    )
    harness = make_harness(project, alloy_home, config=config)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert fake_harnesses.calls_for("judge")
    verifier_checks = [
        check for check in final["checks"] if check["kind"] == "custom"
    ]
    assert len(verifier_checks) == 2
    assert final["verifier_stop"] is not None
    assert any(
        "verifier check budget exhausted" in risk
        for risk in final["verifier_stop"]["remaining_risks"]
    )


async def test_max_total_checks_parks_at_human_gate(
    project, alloy_home, fake_harnesses
):
    """A run-wide check cap parks at the human gate like max_iterations."""
    config = replace(
        load_config(),
        verification=replace(
            load_config().verification,
            max_total_checks=3,
            max_checks_per_iteration=10,
        ),
    )
    fake_harnesses.configure(
        verification_script(
            verifier=[verifier_run_entry('sh -c "exit 0"', kind="custom")] * 20,
        )
    )
    harness = make_harness(project, alloy_home, config=config)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final.get("limit_hit") == "max_total_checks reached"
    assert "__interrupt__" in final
    log_dir = alloy_home / "logs" / harness.run_id
    assert len(list(log_dir.glob("check-*.log"))) <= 3


async def test_verifier_runs_arbitrary_project_script(
    project, alloy_home, fake_harnesses
):
    """The verifier can name any shell command; Alloy runs it without autodetect changes."""
    scripts = project / "scripts"
    scripts.mkdir()
    check_sh = scripts / "check.sh"
    check_sh.write_text("#!/bin/sh\necho ok\nexit 0\n", encoding="utf-8")
    check_sh.chmod(0o755)
    for args in (["add", "-A"], ["commit", "-qm", "add check script"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)

    fake_harnesses.configure(
        verification_script(
            verifier=[
                verifier_run_entry("sh scripts/check.sh", kind="custom"),
                verifier_stop_entry("project script green"),
            ],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["checks"][-1]["command"] == "sh scripts/check.sh"
    assert final["checks"][-1]["exit_code"] == 0


async def test_unrunnable_verifier_command_surfaces_in_next_prompt(
    project, alloy_home, fake_harnesses
):
    """An unrunnable verifier command is reported back on the next verifier turn."""
    fake_harnesses.configure(
        verification_script(
            verifier=[
                verifier_run_entry("definitely-not-a-program"),
                verifier_stop_entry("probe complete"),
            ],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    verifier_calls = fake_harnesses.calls_for("verifier")
    assert len(verifier_calls) >= 2
    assert "could not run" in verifier_calls[1]["prompt"]
    assert "definitely-not-a-program" in verifier_calls[1]["prompt"]


# -- acceptance_gate (alloy-21u.5) ------------------------------------------


def acceptance_script(**overrides):
    """Verifier stop is followed by the acceptance gate; accept bypasses the judge."""
    base = verification_script(
        acceptance=[acceptance_entry("accept", confidence=0.9)],
        judge=[],
    )
    base.update(overrides)
    return base


async def test_verifier_stop_calls_acceptance_before_judge(
    project, alloy_home, fake_harnesses
):
    """(a) After the verifier stops, the acceptance role runs next."""
    fake_harnesses.configure(acceptance_script())
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    roles = [call["role"] for call in fake_harnesses.calls]
    last_verifier = max(i for i, role in enumerate(roles) if role == "verifier")
    assert roles[last_verifier + 1] == "acceptance"
    assert final["outcome"] == "done"
    assert fake_harnesses.calls_for("judge") == []
    assert final["acceptance"]["decision"] == "accept"


async def test_acceptance_verify_more_returns_to_verifier_with_reason(
    project, alloy_home, fake_harnesses
):
    """(b) verify_more re-enters the verifier with the gate's reason in its prompt."""
    gate_reason = "unicode normalization is still unverified"
    fake_harnesses.configure(
        acceptance_script(
            acceptance=[
                acceptance_entry("verify_more", reason=gate_reason),
                acceptance_entry("accept", confidence=0.9),
            ],
            verifier=[
                verifier_run_entry(FULL_SUITE, kind="regression"),
                verifier_stop_entry("first stop"),
                verifier_run_entry(FULL_SUITE, kind="regression"),
                verifier_stop_entry("second stop after verify_more"),
            ],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    roles = [call["role"] for call in fake_harnesses.calls]
    first_acceptance = roles.index("acceptance")
    second_verifier = roles.index("verifier", first_acceptance + 1)
    assert roles[first_acceptance + 1 : second_verifier] == []
    assert gate_reason in fake_harnesses.calls[second_verifier]["prompt"]


async def test_acceptance_repair_routes_to_implement_with_gate_reason(
    project, alloy_home, fake_harnesses
):
    """(c) repair sends the implementer back with the acceptance gate's ask."""
    repair_reason = "slugify must reject empty input"
    fake_harnesses.configure(
        acceptance_script(
            acceptance=[
                acceptance_entry("repair", reason=repair_reason),
                acceptance_entry("accept", confidence=0.9),
            ],
            implement=[implement_entry(succeed=True), implement_entry(succeed=True)],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    roles = [call["role"] for call in fake_harnesses.calls]
    first_acceptance = roles.index("acceptance")
    second_implement = roles.index("implement", first_acceptance + 1)
    assert roles[first_acceptance + 1 : second_implement] == []
    prompt = fake_harnesses.calls_for("implement")[1]["prompt"]
    assert "acceptance gate asked for a repair" in prompt
    assert repair_reason in prompt


async def test_low_confidence_accept_escalates_to_judge(
    project, alloy_home, fake_harnesses
):
    """(d) accept below min_acceptance_confidence escalates to the judge."""
    fake_harnesses.configure(
        acceptance_script(
            acceptance=[acceptance_entry("accept", confidence=0.3)],
            judge=[judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    roles = [call["role"] for call in fake_harnesses.calls]
    acceptance_idx = roles.index("acceptance")
    judge_idx = roles.index("judge")
    assert judge_idx == acceptance_idx + 1
    assert fake_harnesses.calls_for("judge")


async def test_acceptance_escalate_reaches_judge_and_human_gate(
    project, alloy_home, fake_harnesses
):
    """(e) escalate routes to the judge; a human decision still parks the run."""
    human_reason = "which unicode normalization form?"
    fake_harnesses.configure(
        acceptance_script(
            acceptance=[acceptance_entry("escalate", reason="needs author input")],
            judge=[judge_entry("human", human_reason)],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    roles = [call["role"] for call in fake_harnesses.calls]
    acceptance_idx = roles.index("acceptance")
    assert roles[acceptance_idx + 1] == "judge"
    assert "__interrupt__" in final
    assert final["__interrupt__"][0].value["reason"] == human_reason


async def test_acceptance_accept_with_empty_diff_is_overridden_to_retry(
    project, alloy_home, fake_harnesses
):
    """(f) accept cannot finish on an empty diff; guard overrides to retry."""
    fake_harnesses.configure(
        acceptance_script(
            implement=[implement_empty_diff_entry(), implement_entry(succeed=True)],
            verifier=[
                verifier_run_entry('sh -c "exit 0"', kind="custom"),
                verifier_stop_entry("custom check green"),
            ],
            judge=[judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert any(
        "overridden by Alloy" in attempt.get("reason", "")
        for attempt in final["attempts"]
    )


async def test_failed_acceptance_call_escalates_with_default_verdict(
    project, alloy_home, fake_harnesses
):
    """(g) A non-zero acceptance harness exit routes to the judge as escalate."""
    fake_harnesses.configure(
        acceptance_script(
            acceptance=[{"exit": 1, "stderr": "acceptance harness crashed\n"}],
            judge=[judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert fake_harnesses.calls_for("judge")
    assert final["acceptance"]["decision"] == "escalate"


# -- session continuity (alloy-21u.6) ---------------------------------------


FAKE_SESSION = "fake-session"
REPOSITORY_CONTEXT_HEADING = "## Repository context"


def verifier_modify_worktree_entry() -> dict:
    """Verifier harness entry that illegally writes into the worktree."""
    entry = verifier_run_entry('sh -c "exit 0"', kind="custom")
    entry["write"] = [{"path": "verifier-touched.txt", "content": "must not happen"}]
    return entry


async def test_verifier_calls_resume_tests_session_on_happy_path(
    project, alloy_home, fake_harnesses
):
    """(a) Verifier resumes the tests writer's session; resumed prompts omit repo context."""
    fake_harnesses.configure(verification_script())
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    tests_calls = fake_harnesses.calls_for("tests")
    assert tests_calls
    assert REPOSITORY_CONTEXT_HEADING in tests_calls[0]["prompt"]

    verifier_calls = fake_harnesses.calls_for("verifier")
    assert verifier_calls
    for call in verifier_calls:
        assert call.get("resume") == FAKE_SESSION
        assert REPOSITORY_CONTEXT_HEADING not in call["prompt"]


async def test_green_baseline_repair_resumes_tests_session(
    project, alloy_home, fake_harnesses
):
    """(b) A tests repair pass after a green baseline resumes the tests session."""
    fake_harnesses.configure(
        script(tests=[write_tests_entry(passing=True), write_tests_entry()])
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    tests_calls = fake_harnesses.calls_for("tests")
    assert len(tests_calls) == 2
    assert REPOSITORY_CONTEXT_HEADING in tests_calls[0]["prompt"]
    assert tests_calls[1].get("resume") == FAKE_SESSION
    assert REPOSITORY_CONTEXT_HEADING not in tests_calls[1]["prompt"]


async def test_tests_fallback_clears_session_for_later_calls(
    project, alloy_home, fake_harnesses
):
    """(c) After tests fallback, no later harness call attempts session resume."""
    config = load_config()
    roles = dict(config.roles)
    roles["tests"] = replace(
        roles["tests"],
        runner="cursor",
        fallback=RoleSpec(runner="claude-write", model="fable"),
    )
    config = replace(config, roles=roles)
    fake_harnesses.configure(
        script(
            **{
                "tests@cursor-agent": [
                    {"exit": 0, "is_error": True, "text": "rate limited"},
                ],
                "tests@claude": [write_tests_entry()],
            }
        )
    )
    harness = make_harness(project, alloy_home, config=config)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    assert [c["runner"] for c in fake_harnesses.calls_for("tests")] == [
        "cursor-agent",
        "claude",
    ]
    assert final["tests_session"] == {
        "runner": "claude-write",
        "session_id": FAKE_SESSION,
    }
    verifier_calls = fake_harnesses.calls_for("verifier")
    assert verifier_calls
    assert all("resume" not in call for call in verifier_calls)
    verifier_ledger = [
        row for row in harness.store.agent_calls(harness.run_id) if row["role"] == "verifier"
    ]
    assert json.loads(verifier_ledger[0]["usage_json"])["resumed"] is False
    for fn in (tests_prompt, verifier_prompt):
        assert "resumed" in inspect.signature(fn).parameters


async def test_verifier_resume_failure_retries_fresh_then_completes(
    project, alloy_home, fake_harnesses
):
    """(d) A failed resumed verifier call retries fresh; ledger records resumed true then false."""
    fake_harnesses.configure(
        verification_script(
            **{
                "verifier@cursor-agent": [
                    {"exit": 1, "stderr": "resumed verifier failed\n"},
                    verifier_run_entry(FULL_SUITE, kind="regression"),
                    verifier_stop_entry("regression suite green after fresh retry"),
                ],
            }
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    verifier_calls = fake_harnesses.calls_for("verifier")
    assert len(verifier_calls) >= 2
    assert verifier_calls[0].get("resume") == FAKE_SESSION
    assert "resume" not in verifier_calls[1]

    ledger = [
        call for call in harness.store.agent_calls(harness.run_id)
        if call["role"] == "verifier"
    ]
    assert len(ledger) >= 2
    assert json.loads(ledger[0]["usage_json"])["resumed"] is True
    assert json.loads(ledger[1]["usage_json"])["resumed"] is False


async def test_verifier_worktree_mutation_parks_at_human_gate(
    project, alloy_home, fake_harnesses
):
    """(e) A verifier that edits the worktree parks the run for a human decision."""
    fake_harnesses.configure(
        verification_script(
            verifier=[
                verifier_modify_worktree_entry(),
                verifier_stop_entry("should not reach stop after mutation"),
            ],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert "__interrupt__" in final
    reason = final["__interrupt__"][0].value["reason"]
    assert "verifier modified the worktree" in reason


# -- prefix_hash ledger (alloy-4ef.6) ----------------------------------------


def _prefix_hashes(store, run_id: str, role: str) -> list[str]:
    return [row["prefix_hash"] for row in store.agent_calls(run_id) if row["role"] == role]


async def test_implement_and_judge_calls_share_prefix_hash_within_one_run(
    project, alloy_home, fake_harnesses
):
    """Every implement and judge call in one run records the same prefix_hash."""
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[judge_entry("retry", "still failing"), judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        await harness.start()
    finally:
        harness.close()

    implement_hashes = _prefix_hashes(harness.store, harness.run_id, "implement")
    judge_hashes = _prefix_hashes(harness.store, harness.run_id, "judge")
    assert len(implement_hashes) >= 2
    assert len(judge_hashes) >= 2
    assert len(set(implement_hashes)) == 1
    assert len(set(judge_hashes)) == 1


async def test_implement_prefix_hash_matches_across_runs_with_different_bead_briefs(
    project, alloy_home, fake_harnesses
):
    """Two runs on the same repo with different task briefs share implement prefix_hash."""
    db_path = alloy_home / "alloy.db"
    bead_a = make_bead(
        "bead-a",
        description="Add slugify() to mypkg.",
        acceptance_criteria="slugify('Hello World') == 'hello-world'",
    )
    bead_b = make_bead(
        "bead-b",
        description="Add titlecase() to mypkg.",
        acceptance_criteria="titlecase('hello world') == 'Hello World'",
    )
    fake_harnesses.configure(script())

    harness_a = make_harness(project, alloy_home, bead=bead_a, store=Store(db_path), run_id="run-a")
    try:
        await harness_a.start()
    finally:
        harness_a.close()

    fake_harnesses.reset_calls()
    harness_b = make_harness(project, alloy_home, bead=bead_b, store=Store(db_path), run_id="run-b")
    try:
        await harness_b.start()
    finally:
        harness_b.close()

    hash_a = _prefix_hashes(harness_a.store, "run-a", "implement")[0]
    hash_b = _prefix_hashes(harness_b.store, "run-b", "implement")[0]
    assert hash_a == hash_b


# -- alloy:check-hints persistence (alloy-4ef.9) -----------------------------


CHECK_HINTS_KEY = "alloy:check-hints"
TARGETED_PYTEST = "pytest -q tests/test_slugify.py"
REGRESSION_PYTEST = "pytest -q"
AUTODETECT_PYTEST = "python -m pytest -q"
HINTS_HEADING = "## Hints from the repository (not yet verified)"


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


def _workflow_beads(project: Path, fake_workflow: FakeWorkflow) -> BeadsClient:
    return BeadsClient(repo=project, binary=str(fake_workflow.bd))


def _bd_remember_calls(fake_workflow: FakeWorkflow) -> list[dict]:
    return [call for call in fake_workflow.calls if call.get("command") == "remember"]


def _check_hints_remember_calls(fake_workflow: FakeWorkflow) -> list[dict]:
    return [
        call
        for call in _bd_remember_calls(fake_workflow)
        if CHECK_HINTS_KEY in call.get("argv", [])
    ]


def _remember_key_and_body(call: dict) -> tuple[str, str]:
    argv = call["argv"]
    key = argv[argv.index("--key") + 1]
    return key, argv[1]


def _expected_check_hints_body() -> str:
    """Runnable verifier checks, newest first, excluding exit-127 commands."""
    return (
        f"regression: {REGRESSION_PYTEST}\n"
        f"targeted: {TARGETED_PYTEST}"
    )


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


async def test_done_run_remembers_runnable_verifier_checks_as_alloy_check_hints(
    project, alloy_home, fake_workflow
):
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


async def test_failed_run_writes_no_alloy_check_hints(
    project, alloy_home, fake_workflow
):
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


async def test_done_run_skips_remember_when_alloy_check_hints_unchanged(
    project, alloy_home, fake_workflow
):
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
        project, alloy_home, bead=make_bead("t-2"), beads=beads, run_id="run-second",
    )
    try:
        second_final = await second_harness.start()
    finally:
        second_harness.close()

    assert second_final["outcome"] == "done"
    assert first_stripped == _expected_check_hints_body()
    # configure() reset calls.jsonl before the second run: it must log no write.
    assert _check_hints_remember_calls(fake_workflow) == []


async def test_verifier_prompt_lists_remembered_check_hints_before_autodetect(
    project, alloy_home, fake_workflow
):
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
