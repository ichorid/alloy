"""Run limits on every loop edge (tentura-s7gl.5, run 63a032ae...).

The reported-bug repair cycle implement -> triage(blocking) -> implement never
passed through guard, the only node that consulted `check_limits`, so a run
with max_iterations=5 / max_wall_time=90m ran 11 iterations and 102+ minutes.
These tests reproduce that cycle with the fake harnesses and prove every
limit now stops it, through guard's usual needs-human handling.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import timedelta

from conftest import implement_entry, triage_entry
from support import make_bead, make_harness
from test_fast_track import fast_track_config, run_check_entry
from test_triage import RecordingBeadsClient, bug_block, script, triage_config

from alloy.config import RoleSpec
from alloy.runtime import LIMIT_REFUSED_KEY, RunContext


def endless_repair_implement(count: int = 30) -> list[dict]:
    """Every implement call stops on a *new* blocking bug (distinct titles,
    so capture never dedupes them) -- the shape of the s7gl.5 log."""
    return [
        {"text": f"Cannot continue.\n\n{bug_block(f'Blocking defect #{n}', blocks_task='yes')}"}
        for n in range(1, count + 1)
    ]


def limited(config, **limits):
    return replace(config, limits=replace(config.limits, **limits))


def _roles(fake_harnesses) -> list[str]:
    return [call["role"] for call in fake_harnesses.calls]


async def _run_endless(project, alloy_home, fake_harnesses, config):
    fake_harnesses.configure(
        script(
            implement=endless_repair_implement(),
            triage=[triage_entry("blocking", "blocks the task")],
        )
    )
    harness = make_harness(
        project,
        alloy_home,
        config=config,
        bead=make_bead(id="loop-1", metadata={"alloy_recipe": "tdd-loop"}),
        beads=RecordingBeadsClient(bug_ids=[f"bug-{n}" for n in range(40)]),
    )
    try:
        final = await harness.start()
    finally:
        harness.close()
    return harness, final


async def test_endless_bug_repair_loop_stops_at_max_iterations(project, alloy_home, fake_harnesses):
    config = limited(triage_config(), max_iterations=3, max_agent_calls=100, max_cheap_agent_calls=100)
    harness, final = await _run_endless(project, alloy_home, fake_harnesses, config)

    assert "__interrupt__" in final, "the run must park at the human gate, not loop forever"
    payload = final["__interrupt__"][0].value
    assert payload["limit_hit"].startswith("max_iterations reached (3/3)")
    assert "Alloy stopped the loop" in payload["reason"]
    # Each bug-repair iteration counted: exactly max_iterations implement calls.
    assert _roles(fake_harnesses).count("implement") == 3
    assert final["iteration"] == 3
    # The repair path never reached the verifier, yet it stopped.
    assert "verifier" not in _roles(fake_harnesses)


async def test_endless_bug_repair_loop_stops_at_max_agent_calls(project, alloy_home, fake_harnesses):
    config = limited(triage_config(), max_iterations=50, max_agent_calls=4, max_agent_calls_by_tier={}, max_cheap_agent_calls=100)
    harness, final = await _run_endless(project, alloy_home, fake_harnesses, config)

    assert "__interrupt__" in final
    assert final["__interrupt__"][0].value["limit_hit"].startswith("max_agent_calls reached")
    assert harness.store.call_count(harness.run_id) <= 4


async def test_endless_bug_repair_loop_stops_at_max_cheap_agent_calls(project, alloy_home, fake_harnesses):
    config = limited(
        triage_config(),
        max_iterations=50,
        max_agent_calls=100,
        max_cheap_agent_calls=2,
        cheap_roles=("triage",),
    )
    harness, final = await _run_endless(project, alloy_home, fake_harnesses, config)

    assert "__interrupt__" in final
    assert final["__interrupt__"][0].value["limit_hit"].startswith("max_cheap_agent_calls reached")
    assert _roles(fake_harnesses).count("triage") == 2


async def test_endless_bug_repair_loop_stops_at_max_wall_time(project, alloy_home, fake_harnesses, monkeypatch):
    """Wall time "passes" with every implement call: after the second one the
    run is past its 90 minutes, and nothing new starts."""

    def fake_elapsed(self: RunContext) -> timedelta:
        implements = [c for c in self.store.agent_calls(self.run_id) if c["role"] == "implement"]
        return timedelta(minutes=50 * len(implements))

    monkeypatch.setattr(RunContext, "elapsed", fake_elapsed)
    config = limited(triage_config(), max_iterations=50, max_agent_calls=100, max_cheap_agent_calls=100)
    harness, final = await _run_endless(project, alloy_home, fake_harnesses, config)

    assert "__interrupt__" in final
    assert final["__interrupt__"][0].value["limit_hit"].startswith("max_wall_time reached")
    assert _roles(fake_harnesses).count("implement") == 2


async def test_resume_after_limit_grants_one_more_window(project, alloy_home, fake_harnesses):
    """The stop is the ordinary needs-human park: a human resume doubles the
    budget once, and the still-endless loop stops again at the new ceiling."""
    from langgraph.types import Command

    config = limited(triage_config(), max_iterations=2, max_agent_calls=100, max_cheap_agent_calls=100)
    harness, final = await _run_endless(project, alloy_home, fake_harnesses, config)
    assert final["__interrupt__"][0].value["limit_hit"].startswith("max_iterations reached (2/2)")
    try:
        again = await harness.resume(Command(resume={"instructions": "keep going"}))
    finally:
        harness.close()
    assert again["__interrupt__"][0].value["limit_hit"].startswith("max_iterations reached (4/4)")
    assert _roles(fake_harnesses).count("implement") == 4


async def test_bounded_bug_repair_still_finishes_done(project, alloy_home, fake_harnesses):
    """A legitimate repair (one blocking bug, then a fix) inside the limits
    is untouched by the gates."""
    fake_harnesses.configure(
        script(
            implement=[
                {"text": f"Stuck.\n\n{bug_block('Race in worker pool', blocks_task='yes')}"},
                implement_entry(succeed=True),
            ],
            triage=[triage_entry("blocking", "reproduces")],
        )
    )
    harness = make_harness(
        project,
        alloy_home,
        config=limited(triage_config(), max_iterations=3),
        bead=make_bead(id="loop-ok", metadata={"alloy_recipe": "tdd-loop"}),
        beads=RecordingBeadsClient(bug_ids=["bug-ok"]),
    )
    try:
        final = await harness.start()
    finally:
        harness.close()

    assert "__interrupt__" not in final
    assert final["outcome"] == "done"
    assert final["iteration"] == 2  # the repair iteration counted
    assert not final.get("limit_hit")


async def test_fast_track_implement_check_loop_is_bounded(project, alloy_home, fake_harnesses):
    """fast-track's implement -> run_check -> implement cycle has no guard
    either; an endless "check again" loop stops on the run's limits."""
    fake_harnesses.configure({"implement": [run_check_entry(f"{sys.executable} -c \"print(1)\"", reason="again")]})
    config = limited(fast_track_config(), max_iterations=3, max_agent_calls=100, max_agent_calls_by_tier={})
    config = replace(config, verification=replace(config.verification, max_total_checks=6))
    harness = make_harness(
        project,
        alloy_home,
        bead=make_bead(id="ft-loop", metadata={"alloy_recipe": "fast-track"}),
        config=config,
        recipe_name="fast-track",
    )
    try:
        final = await harness.start()
    finally:
        harness.close()
    assert "__interrupt__" in final, final
    assert final["__interrupt__"][0].value["limit_hit"] == "max_total_checks reached"
    assert _roles(fake_harnesses).count("implement") == 6  # one check each; the 7th call is not started


async def test_fast_track_retry_loop_stops_at_max_iterations(project, alloy_home, fake_harnesses):
    from test_fast_track import verdict_entry

    fake_harnesses.configure({"implement": [verdict_entry("retry", reason="not yet")]})
    config = limited(fast_track_config(), max_iterations=3, max_agent_calls=100, max_agent_calls_by_tier={})
    harness = make_harness(
        project,
        alloy_home,
        bead=make_bead(id="ft-retry", metadata={"alloy_recipe": "fast-track"}),
        config=config,
        recipe_name="fast-track",
    )
    try:
        final = await harness.start()
    finally:
        harness.close()
    assert final["__interrupt__"][0].value["limit_hit"].startswith("max_iterations reached (3/3)")
    assert _roles(fake_harnesses).count("implement") == 3


async def test_call_is_refused_once_wall_time_is_spent(project, alloy_home, fake_harnesses, monkeypatch):
    """Before every agent call (fallbacks included) the wall time is checked;
    a refused call leaves no ledger row and spends nothing."""
    fake_harnesses.configure({"implement": [implement_entry(succeed=True)]})
    harness = make_harness(project, alloy_home, config=limited(triage_config(), max_wall_time_minutes=90))
    ctx = harness.context(checkpointer=None)
    spec = RoleSpec(runner="codex", model=None, fallback=RoleSpec(runner="claude", model=None))

    monkeypatch.setattr(RunContext, "elapsed", lambda self: timedelta(minutes=95))
    # No limits consulted yet in this process: no window known, call runs.
    first = await ctx.call("implement", spec, "Implement the smallest change")
    assert not (first.usage or {}).get(LIMIT_REFUSED_KEY)
    assert ctx.check_limits({"iteration": 0}).startswith("max_wall_time reached")

    before = harness.store.call_count(harness.run_id)
    refused = await ctx.call("implement", spec, "Implement the smallest change")
    assert refused.ok is False
    assert refused.usage[LIMIT_REFUSED_KEY] is True
    assert "max_wall_time reached (95m/90m)" in refused.error
    assert harness.store.call_count(harness.run_id) == before
    # harvest runs after the loop has ended and is never refused.
    harvest = await ctx.call("harvest", spec, "You are harvesting a durable lesson")
    assert not (harvest.usage or {}).get(LIMIT_REFUSED_KEY)
    # A human extension (budget x2) lifts the stop.
    assert ctx.check_limits({"iteration": 0, "budget_extensions": 1}) is None
    allowed = await ctx.call("implement", spec, "Implement the smallest change")
    assert not (allowed.usage or {}).get(LIMIT_REFUSED_KEY)


def test_iteration_internal_gate_ignores_max_iterations(project, alloy_home):
    harness = make_harness(project, alloy_home, config=limited(triage_config(), max_iterations=3))
    ctx = harness.context(checkpointer=None)
    assert ctx.check_limits({"iteration": 3}).startswith("max_iterations reached")
    assert ctx.check_limits({"iteration": 3}, include_iterations=False) is None
