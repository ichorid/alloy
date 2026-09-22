"""Workflow routing, limits, consilium and the human gate.

Every test scripts the agents' decisions and then asserts on what Alloy did with
them -- especially where Alloy overrules the agent.
"""

from __future__ import annotations

from dataclasses import replace

from alloy.config import Limits
from conftest import (
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    write_tests_entry,
)
from support import load_config, make_harness


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
        "context", "estimate", "tests", "implement", "judge"
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
    fake_harnesses.configure(script())
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["baseline"]["exit_code"] != 0  # tests were failing before implementation
    assert final["last_tests"]["exit_code"] == 0  # and passing after


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
        script(
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
        "context", "estimate", "tests", "tests", "tests", "implement", "judge"
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
        script(implement=[implement_entry(succeed=False)], judge=[judge_entry("done")])
    )
    harness = make_harness(project, alloy_home, config=config)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["iteration"] == 3
    assert "max_iterations" in final["limit_hit"]
    assert "__interrupt__" in final
