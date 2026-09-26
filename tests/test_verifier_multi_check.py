"""Verifier batch checks (alloy-8by.2): one agent response may name several checks."""

from __future__ import annotations

from alloy.models import CheckRequest, VerifierAction
from conftest import verifier_run_entry, verifier_run_many_entry, verifier_stop_entry
from support import make_harness
from test_workflow import verification_script

CMD_A = 'sh -c "echo batch-a"'
CMD_B = 'sh -c "echo batch-b"'
CMD_C = 'sh -c "echo batch-c"'


def _batch_structured() -> dict:
    return verifier_run_many_entry(
        (CMD_A, "targeted", "bindings"),
        (CMD_B, "targeted", "view"),
        (CMD_C, "lint", "lint"),
    )["structured"]


def test_verifier_action_schema_for_agents_includes_checks_array():
    props = VerifierAction.schema_for_agents()["properties"]
    checks = props["checks"]
    assert checks["type"] == "array"
    item_props = checks["items"]["properties"]
    assert set(item_props) >= {"command", "purpose", "kind", "required"}


def test_verifier_check_requests_expands_batch_response_to_three():
    from alloy.verify import verifier_check_requests

    action = VerifierAction.model_validate(_batch_structured())
    requests = verifier_check_requests(action)
    assert len(requests) == 3
    assert [r.command for r in requests] == [CMD_A, CMD_B, CMD_C]
    assert [r.kind for r in requests] == ["targeted", "targeted", "lint"]


def test_verifier_check_requests_legacy_single_run_response_is_one_request():
    from alloy.verify import verifier_check_requests

    legacy = verifier_run_entry('sh -c "exit 0"', kind="regression")["structured"]
    action = VerifierAction.model_validate(legacy)
    requests = verifier_check_requests(action)
    assert len(requests) == 1
    assert requests[0] == CheckRequest(
        command='sh -c "exit 0"',
        purpose=legacy["purpose"],
        kind="regression",
        required=True,
    )


async def test_verifier_batch_response_records_three_subprocess_runs_in_one_agent_call(
    project,
    alloy_home,
    fake_harnesses,
):
    """One verifier LLM call naming three checks must execute all three before the next."""
    fake_harnesses.configure(
        verification_script(
            verifier=[
                verifier_run_many_entry(
                    (CMD_A, "targeted", "bindings"),
                    (CMD_B, "targeted", "view"),
                    (CMD_C, "lint", "lint"),
                ),
                verifier_stop_entry("batch checks green"),
            ],
        )
    )
    harness = make_harness(project, alloy_home)
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"

    verifier_calls = fake_harnesses.calls_for("verifier")
    assert len(verifier_calls) == 2  # the batch call plus the stop call

    batch_commands = {CMD_A, CMD_B, CMD_C}
    recorded = [c for c in final["checks"] if c["command"] in batch_commands]
    assert len(recorded) == 3
    assert {c["command"] for c in recorded} == batch_commands
    assert all(c["exit_code"] == 0 for c in recorded)

    verifier_rows = [row for row in harness.store.agent_calls(harness.run_id) if row["role"] == "verifier"]
    assert len(verifier_rows) == 2
