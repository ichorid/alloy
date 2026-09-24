"""Pre-dispatch agent-call headroom before remediation (alloy-8by.3)."""

from __future__ import annotations

import json
from dataclasses import replace

from alloy.config import Limits
from alloy.models import Outcome
from conftest import estimate_entry, judge_entry
from support import bind_fake_remediator, make_bead, make_harness
from test_remediate import _interrupt_reason
from test_runtime_per_run_agent_budget import _seed_calls
from test_triage import (
    RecordingBeadsClient,
    implement_stopped_with_bug,
    script,
    triage_config,
    triage_entry,
)

# Medium-tier calibration mean used as the remediation cost estimate once headroom
# gating exists; with max_agent_calls=20 and 14 pre-seeded parent calls, the five
# agent roles before remediate (context, estimate, tests, implement, triage) leave
# one call of headroom, well below this estimate.
_MEDIUM_CALIBRATION_12_CALLS = json.dumps(
    {
        "medium": {
            "runs": 40,
            "mean_iterations": 1.6,
            "mean_agent_calls": 12.0,
            "overruns": 0,
        }
    }
)


async def test_blocking_bug_skips_remediation_when_agent_call_headroom_insufficient(
    project, alloy_home, fake_harnesses
):
    """When remaining parent calls are below the remediation estimate, park immediately."""
    beads = RecordingBeadsClient(bug_ids=["bug-headroom"])
    bead = make_bead(id="parent-headroom", metadata={"alloy_recipe": "tdd-loop"})
    config = replace(
        triage_config(),
        limits=Limits(
            max_iterations=50,
            max_consiliums=0,
            max_agent_calls=20,
            max_wall_time_minutes=90,
        ),
    )
    fake_harnesses.configure(
        script(
            estimate=[estimate_entry(complexity="medium")],
            implement=[
                implement_stopped_with_bug("Race leaves too few calls for remediation"),
            ],
            triage=[triage_entry("blocking", "reproduces on CI")],
            judge=[judge_entry("done")],
        )
    )
    run_id = "parent-headroom-run"
    harness = make_harness(
        project,
        alloy_home,
        config=config,
        bead=bead,
        beads=beads,
        run_id=run_id,
        initial_state_overrides={
            "memory_calibration": _MEDIUM_CALIBRATION_12_CALLS,
            "complexity": "medium",
            "complexity_source": "operator",
        },
    )
    _seed_calls(
        harness.store,
        run_id=run_id,
        bead_id=bead.id,
        count=14,
        prefix="pre",
    )
    remediator = bind_fake_remediator(harness)
    try:
        paused = await harness.start()
    finally:
        harness.close()

    assert remediator.calls == []
    assert "__interrupt__" in paused
    reason = _interrupt_reason(paused)
    assert "agent call headroom" in reason
    assert "bug-headroom" in reason
    assert paused.get("outcome") != Outcome.DONE.value
