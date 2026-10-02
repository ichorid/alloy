"""The fast-track recipe: one role implements, proposes its own checks, and
self-judges done/retry/human. Alloy's own enforcement -- a `done` claim is
never honored unless a real check has actually run this iteration -- is the
whole of "minimal, not zero, verification" for this recipe."""

from __future__ import annotations

from conftest import IMPLEMENTATION
from support import make_bead, make_harness

from alloy.config import load_recipe

PROBE = "test -f mypkg/__init__.py"


def fast_track_config():
    return load_recipe("fast-track")


def verdict_entry(action, *, checks=None, reason="", next_instructions="", write=None, confidence=0.9):
    entry = {
        "text": "Implemented the task.",
        "structured": {
            "action": action,
            "checks": checks or [],
            "reason": reason or action,
            "next_instructions": next_instructions,
            "confidence": confidence,
        },
    }
    if write:
        entry["write"] = write
    return entry


def done_entry(*, checks=None, reason="done", write=None):
    return verdict_entry("done", checks=checks, reason=reason, write=write)


def run_check_entry(command, *, reason="run it", write=None):
    return verdict_entry(
        "run_check",
        checks=[{"command": command, "purpose": "verify the change", "kind": "custom", "required": True}],
        reason=reason,
        write=write,
    )


async def test_fast_track_runs_checks_proposed_alongside_a_premature_done(project, alloy_home, fake_harnesses):
    """A `done` verdict that proposes checks it has not run yet must not be
    honored on the spot: Alloy runs those checks first, and only a later call
    that sees the (passing) result may actually finish the run."""
    fake_harnesses.configure(
        {
            "implement": [
                # Declares done *and* proposes a check in the same answer --
                # Alloy must run it before honoring "done".
                done_entry(
                    checks=[{"command": PROBE, "purpose": "file exists", "kind": "custom", "required": True}],
                    write=[{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}],
                ),
                # Now a check has run this iteration: done is honored.
                done_entry(reason="verified by the check that just ran"),
                # Final broader pass: same story once more.
                run_check_entry(PROBE, reason="final pass check"),
                done_entry(reason="final pass verified"),
            ],
        }
    )
    harness = make_harness(
        project,
        alloy_home,
        bead=make_bead(metadata={"alloy_recipe": "fast-track"}),
        config=fast_track_config(),
        recipe_name="fast-track",
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    checks = final.get("checks") or []
    assert checks, "the proposed check must actually have run before done was honored"
    assert all(check["command"] == PROBE and check["exit_code"] == 0 for check in checks)


async def test_fast_track_refuses_done_with_no_checks_at_all(project, alloy_home, fake_harnesses):
    """A `done` verdict with no checks proposed at all is converted to a
    forced retry asking for a real check -- never silently honored."""
    fake_harnesses.configure(
        {
            "implement": [
                # Claims done outright, proposes nothing to verify it.
                done_entry(write=[{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}]),
                # Now it proposes a real check.
                run_check_entry(PROBE),
                # Check passed this iteration: done is honored.
                done_entry(reason="verified"),
                run_check_entry(PROBE, reason="final pass check"),
                done_entry(reason="final pass verified"),
            ],
        }
    )
    harness = make_harness(
        project,
        alloy_home,
        bead=make_bead(metadata={"alloy_recipe": "fast-track"}),
        config=fast_track_config(),
        recipe_name="fast-track",
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    attempts = final.get("attempts") or []
    assert any(a["decision"] == "retry" and "no check run" in a["reason"] for a in attempts), (
        "the no-checks-at-all done claim must have been overridden to retry"
    )
    assert final["outcome"] == "done"


async def test_fast_track_skips_tests_judge_and_acceptance_roles(project, alloy_home, fake_harnesses):
    """No tests/triage/verifier/acceptance/judge/consilium role ever runs in
    this recipe -- only implement, run_check (no agent call) and harvest."""
    fake_harnesses.configure(
        {
            "implement": [
                done_entry(
                    checks=[{"command": PROBE, "purpose": "file exists", "kind": "custom", "required": True}],
                    write=[{"path": "mypkg/__init__.py", "content": IMPLEMENTATION}],
                ),
                done_entry(reason="verified"),
                run_check_entry(PROBE, reason="final pass check"),
                done_entry(reason="final pass verified"),
            ],
        }
    )
    harness = make_harness(
        project,
        alloy_home,
        bead=make_bead(metadata={"alloy_recipe": "fast-track"}),
        config=fast_track_config(),
        recipe_name="fast-track",
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert final["outcome"] == "done"
    roles_seen = {c["role"] for c in harness.store.agent_calls(harness.run_id)}
    assert roles_seen <= {"implement", "harvest"}
