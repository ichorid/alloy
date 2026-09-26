"""Workflow routing, limits, consilium and the human gate.

Every test scripts the agents' decisions and then asserts on what Alloy did with
them -- especially where Alloy overrules the agent.
"""

from __future__ import annotations

import inspect
import json
import subprocess
from dataclasses import replace

from conftest import (
    acceptance_entry,
    implement_empty_diff_entry,
    implement_entry,
    judge_entry,
    verifier_run_entry,
    verifier_stop_entry,
    write_tests_entry,
)
from support import load_config, make_bead, make_harness
from workflow_support import (
    FULL_SUITE,
    TARGETED_SLUGIFY,
    script,
    verification_script,
)

from alloy.config import RoleSpec
from alloy.recipes.tdd_loop import tests_prompt, verifier_prompt
from alloy.store import Store


async def test_red_required_check_routes_to_implement_without_judge(project, alloy_home, fake_harnesses):
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


async def test_verifier_builds_on_green_targeted_before_regression_and_judge(project, alloy_home, fake_harnesses):
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


async def test_verifier_check_kinds_recorded_in_order_across_iterations(project, alloy_home, fake_harnesses):
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


async def test_max_checks_per_iteration_forces_verifier_stop(project, alloy_home, fake_harnesses):
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
    verifier_checks = [check for check in final["checks"] if check["kind"] == "custom"]
    assert len(verifier_checks) == 2
    assert final["verifier_stop"] is not None
    assert any("verifier check budget exhausted" in risk for risk in final["verifier_stop"]["remaining_risks"])


async def test_max_total_checks_parks_at_human_gate(project, alloy_home, fake_harnesses):
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


async def test_verifier_runs_arbitrary_project_script(project, alloy_home, fake_harnesses):
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


async def test_unrunnable_verifier_command_surfaces_in_next_prompt(project, alloy_home, fake_harnesses):
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


async def test_verifier_stop_calls_acceptance_before_judge(project, alloy_home, fake_harnesses):
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


async def test_acceptance_verify_more_returns_to_verifier_with_reason(project, alloy_home, fake_harnesses):
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


async def test_verify_more_without_new_evidence_escalates_to_judge(project, alloy_home, fake_harnesses):
    """A verifier that stops without running anything cannot loop acceptance forever."""
    fake_harnesses.configure(
        acceptance_script(
            acceptance=[acceptance_entry("verify_more", reason="unverifiable risk")] * 10,
            verifier=[
                verifier_run_entry(FULL_SUITE, kind="regression"),
                verifier_stop_entry("first stop"),
                verifier_stop_entry("nothing new to run"),
                verifier_stop_entry("still nothing"),
            ]
            + [verifier_stop_entry("still nothing")] * 6,
            judge=[judge_entry("done")],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert len(fake_harnesses.calls_for("acceptance")) == 2
    assert len(fake_harnesses.calls_for("judge")) == 1
    assert final["outcome"] == "done"
    second = fake_harnesses.calls_for("acceptance")[1]["prompt"]
    assert "asked for more verification 1 time(s)" in second


async def test_acceptance_repair_routes_to_implement_with_gate_reason(project, alloy_home, fake_harnesses):
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


async def test_low_confidence_accept_escalates_to_judge(project, alloy_home, fake_harnesses):
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


async def test_acceptance_escalate_reaches_judge_and_human_gate(project, alloy_home, fake_harnesses):
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


async def test_acceptance_accept_with_empty_diff_is_overridden_to_retry(project, alloy_home, fake_harnesses):
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
    assert any("overridden by Alloy" in attempt.get("reason", "") for attempt in final["attempts"])


async def test_failed_acceptance_call_escalates_with_default_verdict(project, alloy_home, fake_harnesses):
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


async def test_verifier_calls_resume_tests_session_on_happy_path(project, alloy_home, fake_harnesses):
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


async def test_green_baseline_repair_resumes_tests_session(project, alloy_home, fake_harnesses):
    """(b) A tests repair pass after a green baseline resumes the tests session."""
    fake_harnesses.configure(script(tests=[write_tests_entry(passing=True), write_tests_entry()]))
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


async def test_tests_fallback_clears_session_for_later_calls(project, alloy_home, fake_harnesses):
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
    verifier_ledger = [row for row in harness.store.agent_calls(harness.run_id) if row["role"] == "verifier"]
    assert json.loads(verifier_ledger[0]["usage_json"])["resumed"] is False
    for fn in (tests_prompt, verifier_prompt):
        assert "resumed" in inspect.signature(fn).parameters


async def test_verifier_resume_failure_retries_fresh_then_completes(project, alloy_home, fake_harnesses):
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

    ledger = [call for call in harness.store.agent_calls(harness.run_id) if call["role"] == "verifier"]
    assert len(ledger) >= 2
    assert json.loads(ledger[0]["usage_json"])["resumed"] is True
    assert json.loads(ledger[1]["usage_json"])["resumed"] is False


async def test_verifier_worktree_mutation_parks_at_human_gate(project, alloy_home, fake_harnesses):
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


async def test_implement_and_judge_calls_share_prefix_hash_within_one_run(project, alloy_home, fake_harnesses):
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
