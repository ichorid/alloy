"""End to end: a real bead, a real test suite, scripted agents, run in place."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
from conftest import (
    bd_create,
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    synthesize_entry,
    verifier_run_entry,
    verifier_stop_entry,
    write_tests_entry,
)
from support import load_config
from typer.testing import CliRunner

from alloy import beads as bd
from alloy.cli import app
from alloy.engine import Engine, EngineError


@pytest.fixture
def engine(beads_project, alloy_home, monkeypatch):
    engine = Engine.open(beads_project, alloy_home)
    monkeypatch.setattr(engine, "load_config", lambda name: load_config())
    return engine


def script(**overrides):
    base = {
        "context": context_entry(),
        "tests": write_tests_entry(),
        "implement": [implement_entry(succeed=True)],
        "judge": [judge_entry("done", "tests pass and the diff is right")],
        "critic": critic_entry(),
        "synthesize": synthesize_entry(),
    }
    base.update(overrides)
    return base


async def test_a_successful_run_updates_the_bead_and_runs_in_place(engine, beads_project, fake_harnesses):
    """Every bead runs directly in the primary checkout, on whatever branch is
    already checked out -- no isolated worktree, no separate bead branch."""
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    result = await engine.run(bead_id)

    assert result.outcome == "done"
    bead = engine.beads.show(bead_id)
    assert bead.status == bd.STATUS_DONE
    assert bead.metadata[bd.META_RUN_ID] == result.run_id
    assert bead.metadata[bd.META_BRANCH] == ""
    assert bead.metadata[bd.META_WORKTREE] == str(beads_project)
    assert result.worktree == str(beads_project)


async def test_in_place_run_refuses_to_start_on_a_dirty_primary_checkout(engine, beads_project, fake_harnesses):
    """A fresh in-place start must not risk sweeping someone else's tracked,
    uncommitted edit into the bead's diff/commit via commit_wip's `git add -A`."""
    (beads_project / "mypkg" / "__init__.py").write_text("SOMEONE_ELSES_WIP = True\n", encoding="utf-8")
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    with pytest.raises(EngineError, match="uncommitted tracked changes"):
        await engine.run(bead_id)


async def test_the_bead_is_claimed_before_any_agent_runs(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    await engine.run(bead_id)

    assert engine.beads.ready() == []  # it is no longer offered to anyone else


async def test_a_run_is_recorded_with_every_agent_call(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    result = await engine.run(bead_id)
    calls = engine.store.agent_calls(result.run_id)

    # The final broader pass (Alloy's replacement for a separate landing
    # step) re-enters verifier/acceptance/judge once more before harvest.
    assert [call["role"] for call in calls] == [
        "context",
        "estimate",
        "tests",
        "implement",
        "verifier",
        "acceptance",
        "judge",
        "verifier",
        "acceptance",
        "judge",
        "harvest",
    ]
    for call in calls:
        assert call["prompt_hash"]
        assert call["log_path"]
        assert call["duration_s"] >= 0
    record = engine.store.get_run(result.run_id)
    assert record["status"] == "done"
    # Cheap roles (estimate, verifier, acceptance, judge, harvest) spend their
    # own budget; context, tests and implement spend max_agent_calls.
    assert record["agent_calls"] == 3
    assert record["cheap_agent_calls"] == 8


async def test_failure_marks_the_bead_failed_and_keeps_the_worktree(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False)],
            judge=[judge_entry("abort", "cannot be done as specified")],
        )
    )
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    result = await engine.run(bead_id)

    assert result.outcome == "failed"
    assert engine.beads.show(bead_id).status == bd.STATUS_FAILED
    from pathlib import Path

    assert Path(result.worktree).is_dir()  # left for inspection


async def test_human_gate_parks_the_bead_and_resume_completes_it(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False), implement_entry(succeed=True)],
            judge=[
                judge_entry("human", "which unicode normalization?"),
                judge_entry("done"),
            ],
        )
    )
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    paused = await engine.run(bead_id)

    assert paused.outcome == "waiting-human"
    assert paused.interrupt["reason"] == "which unicode normalization?"
    assert engine.beads.show(bead_id).status == bd.STATUS_WAITING_HUMAN
    assert engine.store.get_run(paused.run_id)["status"] == "waiting-human"

    resumed = await engine.resume(bead_id, "use NFKD")

    assert resumed.outcome == "done"
    assert resumed.run_id == paused.run_id  # same run, not a new one
    assert engine.beads.show(bead_id).status == bd.STATUS_DONE
    assert "use NFKD" in fake_harnesses.calls_for("implement")[1]["prompt"]


async def test_tests_role_session_limit_parks_with_retry_at(engine, beads_project, fake_harnesses):
    """A harness 'resets H:MMam/pm' message schedules timed auto-resume metadata."""
    limit_msg = "You've hit your session limit · resets 1:20am"
    fake_harnesses.configure(script(tests=[{"exit": 1, "stderr": limit_msg}]))
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    paused = await engine.run(bead_id)

    assert paused.outcome == "waiting-human"
    assert "resets 1:20am" in paused.interrupt["reason"]
    assert paused.interrupt.get("retry_at")
    datetime.fromisoformat(paused.interrupt["retry_at"])

    record = engine.store.get_run(paused.run_id)
    assert record["status"] == "waiting-human"
    assert record.get("retry_at")
    datetime.fromisoformat(record["retry_at"])


async def test_a_bead_without_a_recipe_is_refused(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "no recipe here")

    with pytest.raises(EngineError, match="no recipe"):
        await engine.run(bead_id)


async def test_a_bead_that_is_not_ready_is_refused(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")
    engine.beads.set_status(bead_id, bd.STATUS_FAILED)

    with pytest.raises(EngineError, match="only 'open'"):
        await engine.run(bead_id)


async def test_cancel_returns_the_bead_to_ready_and_keeps_the_worktree(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False)],
            judge=[judge_entry("human", "need a decision")],
        )
    )
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")
    paused = await engine.run(bead_id)

    assert engine.cancel(bead_id) is True

    assert engine.beads.show(bead_id).status == bd.STATUS_READY
    assert engine.store.get_run(paused.run_id)["status"] == "cancelled"
    assert engine.cancel(bead_id) is False


async def test_status_output_is_machine_readable(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(
        script(
            verifier=[
                verifier_run_entry(f"{sys.executable} -m pytest -q", kind="regression"),
                verifier_stop_entry("suite green"),
            ]
        )
    )
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    result = await engine.run(bead_id)

    from alloy.cli import _status_row

    row = _status_row(engine, engine.store.get_run(result.run_id))

    assert row["bead"] == bead_id
    assert row["recipe"] == "tdd-loop"
    assert row["status"] == "done"
    assert row["iteration"] == 1
    assert row["max_iterations"] == 5
    assert row["tests"] == "1 checks, last: 1 passed, 0 failed"
    # A finished run has no current agent; the column must not keep naming
    # whichever call happened to be last.
    assert row["stage"] == "finished"
    assert row["agent_role"] is None
    assert row["runner"] is None
    assert row["model"] is None
    assert row["elapsed"].endswith("m")


async def test_status_json_includes_checks_for_finished_run(engine, beads_project, alloy_home, fake_harnesses):
    fake_harnesses.configure(
        script(
            verifier=[
                verifier_run_entry(f"{sys.executable} -m pytest -q", kind="regression"),
                verifier_stop_entry("suite green"),
            ]
        )
    )
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    result = await engine.run(bead_id)

    snapshot = engine.graph_snapshot_for_run(result.run_id)
    final_checks = snapshot["values"]["checks"]

    runner = CliRunner()
    cli_result = runner.invoke(
        app,
        [
            "status",
            bead_id,
            "--json",
            "--repo",
            str(beads_project),
            "--root",
            str(alloy_home),
        ],
    )

    assert cli_result.exit_code == 0
    row = json.loads(cli_result.stdout)["beads"][0]
    assert "checks" in row
    assert row["checks"]["total"] == len(final_checks)
    assert row["checks"]["last"]["exit_code"] == 0
    assert row["checks"]["last"]["command"]
    assert row["checks"]["last"]["kind"]
    assert row["checks"]["last"]["headline"]


async def test_rerunning_a_cancelled_bead_starts_from_a_clean_graph(engine, beads_project, fake_harnesses):
    """A new run must not inherit the abandoned run's graph state."""
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False)],
            judge=[judge_entry("human", "need a decision")],
        )
    )
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    abandoned = await engine.run(bead_id)
    engine.cancel(bead_id)

    fake_harnesses.reset_calls()
    fake_harnesses.configure(script())

    result = await engine.run(bead_id)

    assert result.run_id != abandoned.run_id
    assert result.outcome == "done"
    assert [call["role"] for call in fake_harnesses.calls] == [
        "context",
        "estimate",
        "tests",
        "implement",
        "verifier",
        "acceptance",
        "judge",
        "verifier",
        "acceptance",
        "judge",
        "harvest",
    ]
    assert engine.store.get_run(result.run_id)["iteration"] == 1


async def test_graph_snapshot_for_run_returns_that_specific_runs_state(engine, beads_project, fake_harnesses):
    """A bead with two recorded runs must not have `graph_snapshot_for_run` collapse
    to whichever run happens to be latest -- each run keeps its own checkpoint."""
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False)],
            judge=[judge_entry("human", "need a decision")],
        )
    )
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    first = await engine.run(bead_id)
    engine.cancel(bead_id)

    fake_harnesses.reset_calls()
    fake_harnesses.configure(script())
    second = await engine.run(bead_id)

    assert first.run_id != second.run_id

    first_snapshot = engine.graph_snapshot_for_run(first.run_id)
    second_snapshot = engine.graph_snapshot_for_run(second.run_id)

    assert first_snapshot is not None
    assert second_snapshot is not None
    # The human interrupt retains the preceding graph node's stage;
    # "waiting-human" is the run ledger's stage, not checkpoint state.
    assert first_snapshot["values"]["stage"] == "guard"
    assert second_snapshot["values"]["stage"] == "finished"
    assert second_snapshot["values"]["outcome"] == "done"
    assert first_snapshot != second_snapshot

    # graph_snapshot(bead_id) is untouched and still resolves to the *latest* run --
    # graph_snapshot_for_run exists precisely because that is not what the
    # first run's own state should be read from.
    assert engine.graph_snapshot(bead_id) == second_snapshot


async def test_graph_snapshot_for_run_is_none_for_an_unknown_run_id(engine, beads_project):
    assert engine.graph_snapshot_for_run("no-such-run") is None


async def test_resume_reconciles_inflight_calls_before_reassigning_pid(
    engine, beads_project, fake_harnesses, monkeypatch
):
    """Per the plan: `Engine._execute`'s resume path must call
    `Store.reconcile_inflight()` before it overwrites `runs.pid` with the current
    process's pid -- otherwise a stale in-flight row becomes indistinguishable
    from a fresh one, since the run now looks alive again."""
    fake_harnesses.configure(
        script(
            implement=[implement_entry(succeed=False)],
            judge=[judge_entry("human", "need a decision")],
        )
    )
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    await engine.run(bead_id)

    order: list[str] = []
    original_reconcile = engine.store.reconcile_inflight
    original_update_run = engine.store.update_run

    def spy_reconcile():
        order.append("reconcile")
        return original_reconcile()

    def spy_update_run(run_id, **fields):
        if "pid" in fields:
            order.append("pid_reassign")
        return original_update_run(run_id, **fields)

    monkeypatch.setattr(engine.store, "reconcile_inflight", spy_reconcile)
    monkeypatch.setattr(engine.store, "update_run", spy_update_run)

    fake_harnesses.configure(script())
    await engine.resume(bead_id, "use a fix")

    assert "reconcile" in order
    assert "pid_reassign" in order
    assert order.index("reconcile") < order.index("pid_reassign")


async def test_a_fresh_run_enqueues_no_stale_status_write_from_its_pre_claim_read(
    engine, beads_project, fake_harnesses
):
    """`run()` read the bead (`open`) before claiming it. Dispatch must use
    what bd says *after* the claim, or it enqueues `implementing if open` --
    a write that would re-claim the bead if a human reopened it meanwhile."""
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    result = await engine.run(bead_id)

    with engine.store.connect() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM bd_outbox WHERE run_id = ? AND kind = 'status'", (result.run_id,)
        ).fetchall()
    targets = [json.loads(row["payload_json"])["status"] for row in rows]
    assert bd.STATUS_IMPLEMENTING not in targets
    assert targets == [bd.STATUS_DONE]


async def test_taking_over_an_implementing_bead_stamps_the_new_run_as_owner(engine, beads_project, fake_harnesses):
    """No live row, bd already `implementing` (stamped by some earlier run):
    the takeover is claimed CAS-on-`implementing` too, so bd names the new
    run -- which is what fences off the old owner's stale outbox rows."""
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    assert engine.beads.claim(bead_id, run_id="long-gone") is True

    result = await engine.run(bead_id)

    assert result.outcome == "done"
    bead = engine.beads.show(bead_id)
    assert bead.metadata[bd.META_RUN_ID] == result.run_id
    assert bead.status == bd.STATUS_DONE


async def test_an_empty_baseline_parks_naming_reroute_and_reroute_hands_it_to_fast_track(
    engine, beads_project, alloy_home, fake_harnesses
):
    """tentura-brd4.18 and two more: a tdd-loop bead with nothing to prove red
    parks on "no baseline command given"; `alloy reroute` does what the
    operator did by hand -- pin fast-track, cancel the run, leave it ready."""
    fake_harnesses.configure(script(tests=[write_tests_entry(baseline_checks=[])]))
    bead_id = bd_create(beads_project, "audit the slug rules", alloy_recipe="tdd-loop")

    paused = await engine.run(bead_id)

    assert paused.outcome == "waiting-human"
    assert paused.interrupt["reason"].startswith("baseline not runnable: no baseline command given")
    assert f"alloy reroute {bead_id} fast-track" in paused.interrupt["reason"]
    assert f"alloy reroute {bead_id} fast-track" in paused.interrupt["question"]

    cli = CliRunner().invoke(
        app, ["reroute", bead_id, "--json", "--repo", str(beads_project), "--root", str(alloy_home)]
    )

    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.stdout) == {"bead": bead_id, "recipe": "fast-track", "cancelled": True, "status": "open"}
    bead = engine.beads.show(bead_id)
    assert bead.recipe == "fast-track"
    assert bead.status == bd.STATUS_READY
    assert engine.store.get_run(paused.run_id)["status"] == "cancelled"
    assert [b.id for b in engine.beads.ready()] == [bead_id]


def test_reroute_refuses_human_operated_beads_and_unknown_recipes(engine, beads_project):
    from alloy.reconcile import reroute

    gate = bd_create(beads_project, "merge gate", alloy_recipe="tdd-loop", alloy_manual="true")
    with pytest.raises(EngineError, match="human-operated"):
        reroute(engine, gate, "fast-track")
    assert engine.beads.show(gate).recipe == "tdd-loop"

    idle = bd_create(beads_project, "never ran", alloy_recipe="tdd-loop")
    with pytest.raises(EngineError):
        reroute(engine, idle, "no-such-recipe")
    assert engine.beads.show(idle).recipe == "tdd-loop"
    assert reroute(engine, idle, "fast-track") is False  # nothing to cancel; just pinned
    assert engine.beads.show(idle).recipe == "fast-track"
