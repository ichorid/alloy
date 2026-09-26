"""End to end: a real bead, a real worktree, a real test suite, scripted agents."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alloy import beads as bd
from alloy.checkpoints import open_checkpointer
from alloy.cli import app
from alloy.engine import Engine, EngineError
from alloy.models import parse_provenance
from alloy.worktree import Worktree, WorktreeManager, branch_name
from support import load_config
from test_beads import _create_child, _create_epic
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


async def test_a_successful_run_updates_the_bead_and_leaves_a_branch(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    result = await engine.run(bead_id)

    assert result.outcome == "done"
    bead = engine.beads.show(bead_id)
    assert bead.status == bd.STATUS_REVIEW_READY
    assert bead.metadata[bd.META_RUN_ID] == result.run_id
    assert bead.metadata[bd.META_BRANCH] == f"alloy/{bead_id}"
    assert bead.metadata[bd.META_WORKTREE] == result.worktree


async def test_the_bead_is_claimed_before_any_agent_runs(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    await engine.run(bead_id)

    assert engine.beads.ready() == []  # it is no longer offered to anyone else


async def test_agents_only_ever_touch_the_worktree(engine, beads_project, fake_harnesses):
    from pathlib import Path

    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    result = await engine.run(bead_id)

    assert (beads_project / "mypkg" / "__init__.py").read_text() == ""
    assert "slugify" in (Path(result.worktree) / "mypkg" / "__init__.py").read_text()
    for call in fake_harnesses.calls:
        assert call["cwd"] == result.worktree


async def test_a_run_is_recorded_with_every_agent_call(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(script())
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")

    result = await engine.run(bead_id)
    calls = engine.store.agent_calls(result.run_id)

    assert [call["role"] for call in calls] == [
        "context",
        "estimate",
        "tests",
        "implement",
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
    assert record["agent_calls"] == 8


async def test_failure_marks_the_bead_failed_and_keeps_the_worktree(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(
        script(implement=[implement_entry(succeed=False)], judge=[judge_entry("abort", "cannot be done as specified")])
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
            judge=[judge_entry("human", "which unicode normalization?"), judge_entry("done")],
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
    assert engine.beads.show(bead_id).status == bd.STATUS_REVIEW_READY
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
        script(implement=[implement_entry(succeed=False)], judge=[judge_entry("human", "need a decision")])
    )
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")
    paused = await engine.run(bead_id)

    assert engine.cancel(bead_id) is True

    assert engine.beads.show(bead_id).status == bd.STATUS_READY
    assert engine.store.get_run(paused.run_id)["status"] == "cancelled"
    assert engine.cancel(bead_id) is False


async def test_two_beads_get_independent_worktrees(engine, beads_project, fake_harnesses):
    fake_harnesses.configure(script())
    first = bd_create(beads_project, "first", alloy_recipe="tdd-loop")
    second = bd_create(beads_project, "second", alloy_recipe="tdd-loop")

    first_result = await engine.run(first)
    fake_harnesses.reset_calls()
    second_result = await engine.run(second)

    assert first_result.worktree != second_result.worktree
    assert engine.beads.show(first).metadata[bd.META_BRANCH] == f"alloy/{first}"
    assert engine.beads.show(second).metadata[bd.META_BRANCH] == f"alloy/{second}"


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
        ["status", bead_id, "--json", "--repo", str(beads_project), "--root", str(alloy_home)],
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
        script(implement=[implement_entry(succeed=False)], judge=[judge_entry("human", "need a decision")])
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
        "harvest",
    ]
    assert engine.store.get_run(result.run_id)["iteration"] == 1


async def test_graph_snapshot_for_run_returns_that_specific_runs_state(engine, beads_project, fake_harnesses):
    """A bead with two recorded runs must not have `graph_snapshot_for_run` collapse
    to whichever run happens to be latest -- each run keeps its own checkpoint."""
    fake_harnesses.configure(
        script(implement=[implement_entry(succeed=False)], judge=[judge_entry("human", "need a decision")])
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
        script(implement=[implement_entry(succeed=False)], judge=[judge_entry("human", "need a decision")])
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


# -- alloy:regression from unmerged remediations (alloy-4ef.13) ---------------


REGRESSION_PREFIX_KEY = "alloy:regression:src"
BUG_WHERE = "src/alloy/verify.py:12"
BUG_TITLE = "verify() mishandles empty input"


def _bug_description_with_where(title: str, where: str) -> str:
    return (
        f"title: {title}\n"
        f"where: {where}\n"
        f"evidence: merge gate rejected the fix\n"
        f"blocks_task (reporter's opinion): True\n"
    )


async def test_unmerged_remediation_remembers_alloy_regression_prefix(
    engine,
    beads_project,
    fake_harnesses,
):
    """Unmerged remediation writes alloy:regression:<top-level path> with bug title."""
    from test_engine_run_child import (
        _bug_script,
        _parent_context,
        _paused_parent_with_wip,
    )

    parent_id, parent_run_id, _, _ = await _paused_parent_with_wip(
        engine,
        beads_project,
        fake_harnesses,
    )
    bug_id = bd_create(beads_project, BUG_TITLE, alloy_recipe="tdd-loop")
    engine.beads.claim(bug_id)
    subprocess.run(
        ["bd", "update", bug_id, "-d", _bug_description_with_where(BUG_TITLE, BUG_WHERE)],
        cwd=str(beads_project),
        check=True,
        capture_output=True,
        text=True,
    )

    async def gate_reject(bead, diff: str):
        return False, "too-broad: touches verify internals"

    fake_harnesses.reset_calls()
    fake_harnesses.configure(_bug_script())

    async with open_checkpointer(engine.paths.workflows_db) as checkpointer:
        parent_ctx = await _parent_context(engine, parent_id, parent_run_id, checkpointer)
        result = await engine.run_child(
            bug_id,
            parent=parent_ctx,
            merge_gate=gate_reject,
        )

    assert result.outcome == "failed"

    memories = engine.beads.memories()
    assert REGRESSION_PREFIX_KEY in memories, (
        f"expected {REGRESSION_PREFIX_KEY} after unmerged remediation; got keys: {sorted(memories)}"
    )
    body, run_id, bead_id, at = parse_provenance(memories[REGRESSION_PREFIX_KEY])
    assert BUG_TITLE in body
    assert run_id == result.run_id
    assert bead_id == bug_id
    assert at is not None


# -- epic shared worktrees (alloy-vrh.4) ------------------------------------


def _git(worktree_path: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(worktree_path),
        check=True,
        capture_output=True,
        text=True,
    )


def _tag_recipe(beads: bd.BeadsClient, bead_id: str, recipe: str = "tdd-loop") -> None:
    beads.set_metadata(bead_id, {bd.META_RECIPE: recipe})


def _epic_child(beads: bd.BeadsClient, epic_id: str, title: str) -> str:
    child_id = _create_child(beads, title, epic_id)
    _tag_recipe(beads, child_id)
    return child_id


def _implement_write(path: str, content: str) -> dict:
    return {
        "text": f"Wrote {path}",
        "write": [{"path": path, "content": content}],
    }


def _commit_all(worktree_path: Path, message: str) -> str:
    _git(worktree_path, "add", "-A")
    _git(worktree_path, "commit", "-q", "--no-verify", "-m", message)
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(worktree_path),
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _worktree_from_run(
    engine: Engine,
    run_id: str,
    *,
    owner_id: str,
) -> Worktree:
    record = engine.store.get_run(run_id)
    assert record is not None
    assert record.get("base_commit"), "run record must persist base_commit at child start"
    return Worktree(
        bead_id=owner_id,
        path=Path(record["worktree"]),
        branch=record["branch"],
        base_commit=record["base_commit"],
    )


async def test_epic_child_runs_in_shared_epic_worktree(
    engine,
    beads_project,
    fake_harnesses,
):
    """A child under an epic uses <worktrees>/<epic-id> on branch alloy/<epic-id>."""
    epic_id = _create_epic(engine.beads, "OAuth login", "Ship OAuth for the API")
    child_id = _epic_child(engine.beads, epic_id, "add token endpoint")

    fake_harnesses.configure(script())
    result = await engine.run(child_id)

    epic_worktree = engine.paths.worktree_for(epic_id)
    bead = engine.beads.show(child_id)

    assert result.outcome == "done"
    assert Path(result.worktree) == epic_worktree
    assert bead.metadata[bd.META_WORKTREE] == str(epic_worktree)
    assert bead.metadata[bd.META_BRANCH] == branch_name(epic_id)


async def test_epic_child_base_commit_starts_after_sibling_commit(
    engine,
    beads_project,
    fake_harnesses,
):
    """After sibling Y commits on alloy/<epic>, child X's base_commit is Y's HEAD."""
    epic_id = _create_epic(engine.beads, "OAuth login", "Ship OAuth for the API")
    child_y = _epic_child(engine.beads, epic_id, "wire callback route")
    child_x = _epic_child(engine.beads, epic_id, "add token endpoint")

    fake_harnesses.configure(script(implement=[_implement_write("mypkg/y_marker.py", "Y = 1\n")]))
    await engine.run(child_y)

    epic_worktree = engine.paths.worktree_for(epic_id)
    manager = WorktreeManager(repo=engine.repo, root=engine.paths.worktrees)
    # _settle committed Y's work on the owner branch; X's base is that HEAD.
    sibling_head = manager.head(epic_worktree)

    fake_harnesses.reset_calls()
    fake_harnesses.configure(script(implement=[_implement_write("mypkg/x_marker.py", "X = 1\n")]))
    x_result = await engine.run(child_x)

    record = engine.store.get_run(x_result.run_id)
    assert record is not None
    assert record.get("base_commit") == sibling_head


async def test_epic_child_judge_diff_lists_only_this_childs_files(
    engine,
    beads_project,
    fake_harnesses,
):
    """Sibling files already on alloy/<epic> must not appear in X's changed_files/diff."""
    epic_id = _create_epic(engine.beads, "OAuth login", "Ship OAuth for the API")
    child_y = _epic_child(engine.beads, epic_id, "wire callback route")
    child_x = _epic_child(engine.beads, epic_id, "add token endpoint")

    fake_harnesses.configure(script(implement=[_implement_write("mypkg/y_marker.py", "Y = 1\n")]))
    await engine.run(child_y)
    # _settle already committed Y's marker on alloy/<epic>; no manual commit.

    fake_harnesses.reset_calls()
    fake_harnesses.configure(script(implement=[_implement_write("mypkg/x_marker.py", "X = 1\n")]))
    x_result = await engine.run(child_x)

    manager = WorktreeManager(repo=engine.repo, root=engine.paths.worktrees)
    worktree = _worktree_from_run(engine, x_result.run_id, owner_id=epic_id)

    changed = manager.changed_files(worktree)
    diff = manager.diff(worktree)

    assert changed == ["mypkg/x_marker.py"]
    assert "y_marker" not in diff


async def test_worktree_owner_metadata_runs_in_owner_worktree(
    engine,
    beads_project,
    fake_harnesses,
):
    """A bead with alloy_worktree_owner=<id> runs in <worktrees>/<id>."""
    owner_id = bd_create(beads_project, "landed feature", alloy_recipe="tdd-loop")
    fake_harnesses.configure(script())
    await engine.run(owner_id)

    owner_worktree = engine.paths.worktree_for(owner_id)
    repair_id = bd_create(
        beads_project,
        "fix landing conflict",
        alloy_recipe="tdd-loop",
        alloy_worktree_owner=owner_id,
    )

    fake_harnesses.reset_calls()
    # The owner's slugify implementation is still in the shared worktree, so the
    # default script's baseline (slugify tests) would be green and prove_red
    # would park the run at the human gate. The repair bead needs its own red
    # baseline: a new failing test its implement step then turns green.
    repair_tests = write_tests_entry(
        baseline_checks=[
            {
                "command": f"{sys.executable} -m pytest -q tests/test_shout.py",
                "purpose": "Confirm shout tests fail before implementation",
            }
        ],
    )
    repair_tests["write"].append(
        {
            "path": "tests/test_shout.py",
            "content": "from mypkg.shout import shout\n\n\ndef test_shout():\n    assert shout('hi') == 'HI!'\n",
        }
    )
    fake_harnesses.configure(
        script(
            tests=repair_tests,
            implement=[
                _implement_write(
                    "mypkg/shout.py",
                    "def shout(text: str) -> str:\n    return text.upper() + '!'\n",
                )
            ],
        )
    )
    result = await engine.run(repair_id)

    assert result.outcome == "done"
    assert Path(result.worktree) == owner_worktree
    assert engine.beads.show(repair_id).metadata[bd.META_WORKTREE] == str(owner_worktree)


async def test_standalone_bead_keeps_per_bead_worktree_path(
    engine,
    beads_project,
    fake_harnesses,
):
    """Beads without an epic ancestor still use <worktrees>/<own-id>."""
    bead_id = bd_create(beads_project, "standalone task", alloy_recipe="tdd-loop")

    fake_harnesses.configure(script())
    result = await engine.run(bead_id)

    assert result.outcome == "done"
    assert Path(result.worktree) == engine.paths.worktree_for(bead_id)
    assert engine.beads.show(bead_id).metadata[bd.META_BRANCH] == branch_name(bead_id)


async def test_epic_child_resume_restores_recorded_base_commit(
    engine,
    beads_project,
    fake_harnesses,
):
    """Resume must not recompute base via merge-base after more commits land on alloy/<epic>."""
    epic_id = _create_epic(engine.beads, "OAuth login", "Ship OAuth for the API")
    child_y = _epic_child(engine.beads, epic_id, "wire callback route")
    child_x = _epic_child(engine.beads, epic_id, "add token endpoint")

    fake_harnesses.configure(script(implement=[_implement_write("mypkg/y_marker.py", "Y = 1\n")]))
    await engine.run(child_y)
    # _settle committed Y's work on the owner branch; X's base is that HEAD.
    manager = WorktreeManager(repo=engine.repo, root=engine.paths.worktrees)
    sibling_head = manager.head(engine.paths.worktree_for(epic_id))

    fake_harnesses.reset_calls()
    fake_harnesses.configure(
        script(
            implement=[
                _implement_write("mypkg/x_marker.py", "X = 1\n"),
                _implement_write("mypkg/x_marker.py", "X = 2\n"),
            ],
            judge=[judge_entry("human", "confirm approach"), judge_entry("done")],
        )
    )
    paused = await engine.run(child_x)
    assert paused.outcome == "waiting-human"

    recorded_base = engine.store.get_run(paused.run_id).get("base_commit")
    assert recorded_base == sibling_head

    _commit_all(engine.paths.worktree_for(epic_id), "rogue commit while paused")

    fake_harnesses.reset_calls()
    # Resuming from the human gate routes back to the implementer; keep its
    # rewrite on the child's own file so the diff stays exactly this child's.
    fake_harnesses.configure(script(implement=[_implement_write("mypkg/x_marker.py", "X = 3\n")]))
    resumed = await engine.resume(child_x, "proceed")

    assert resumed.outcome == "done"
    assert resumed.run_id == paused.run_id

    run_record = engine.store.get_run(paused.run_id)
    assert run_record.get("base_commit") == recorded_base

    manager = WorktreeManager(repo=engine.repo, root=engine.paths.worktrees)
    worktree = _worktree_from_run(engine, paused.run_id, owner_id=epic_id)
    assert manager.changed_files(worktree) == ["mypkg/x_marker.py"]


# -- epic child settle (alloy-vrh.5) ----------------------------------------


def _latest_commit_message(worktree_path: Path) -> str:
    proc = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=str(worktree_path),
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _worktree_is_clean(worktree_path: Path) -> bool:
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree_path),
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() == ""


def _files_in_head_commit(worktree_path: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=str(worktree_path),
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


async def test_epic_child_success_commits_on_owner_branch_and_closes(
    engine,
    beads_project,
    fake_harnesses,
):
    """Successful epic child: commit on alloy/<epic>, child closed, owner tree kept clean."""
    epic_id = _create_epic(engine.beads, "OAuth login", "Ship OAuth for the API")
    child_id = _epic_child(engine.beads, epic_id, "add token endpoint")
    child_path = "mypkg/token.py"
    child_content = "TOKEN = 'abc'\n"

    epic_worktree = engine.paths.worktree_for(epic_id)

    fake_harnesses.configure(script(implement=[_implement_write(child_path, child_content)]))
    result = await engine.run(child_id)

    bead = engine.beads.show(child_id)
    assert result.outcome == "done"
    assert epic_worktree.is_dir()
    assert _latest_commit_message(epic_worktree).startswith(f"{child_id}:")
    assert child_path in _files_in_head_commit(epic_worktree)
    assert bead.status == bd.STATUS_DONE
    assert _worktree_is_clean(epic_worktree)


async def test_worktree_owner_bead_success_commits_on_owner_branch_and_closes(
    engine,
    beads_project,
    fake_harnesses,
):
    """Beads with alloy_worktree_owner commit on the owner branch and close."""
    owner_id = bd_create(beads_project, "landed feature", alloy_recipe="tdd-loop")
    fake_harnesses.configure(script())
    await engine.run(owner_id)

    owner_worktree = engine.paths.worktree_for(owner_id)
    repair_id = bd_create(
        beads_project,
        "fix landing conflict",
        alloy_recipe="tdd-loop",
        alloy_worktree_owner=owner_id,
    )
    repair_path = "mypkg/repair.py"
    repair_content = "FIXED = True\n"

    repair_tests = write_tests_entry(
        baseline_checks=[
            {
                "command": f"{sys.executable} -m pytest -q tests/test_repair_marker.py",
                "purpose": "Confirm repair marker tests fail before implementation",
            }
        ],
    )
    repair_tests["write"].append(
        {
            "path": "tests/test_repair_marker.py",
            "content": "from mypkg.repair import fixed\n\n\ndef test_repair_marker():\n    assert fixed() is True\n",
        }
    )
    fake_harnesses.reset_calls()
    fake_harnesses.configure(
        script(
            tests=repair_tests,
            implement=[
                _implement_write(repair_path, repair_content),
                _implement_write(
                    "mypkg/repair.py",
                    "def fixed() -> bool:\n    return True\n",
                ),
            ],
        )
    )
    result = await engine.run(repair_id)

    bead = engine.beads.show(repair_id)
    assert result.outcome == "done"
    assert owner_worktree.is_dir()
    assert _latest_commit_message(owner_worktree).startswith(f"{repair_id}:")
    assert "mypkg/repair.py" in _files_in_head_commit(owner_worktree)
    assert bead.status == bd.STATUS_DONE
    assert _worktree_is_clean(owner_worktree)


async def test_standalone_bead_success_stays_review_ready(
    engine,
    beads_project,
    fake_harnesses,
):
    """Beads without a shared owner still land at review-ready with worktree kept."""
    bead_id = bd_create(beads_project, "standalone task", alloy_recipe="tdd-loop")

    fake_harnesses.configure(script())
    result = await engine.run(bead_id)

    bead = engine.beads.show(bead_id)
    assert result.outcome == "done"
    assert bead.status == bd.STATUS_REVIEW_READY
    assert engine.paths.worktree_for(bead_id).is_dir()
