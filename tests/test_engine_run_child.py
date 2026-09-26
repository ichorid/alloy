"""Engine.run_child: remediate a bug bead from a paused parent run (alloy-0uc.8)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from alloy import beads as bd
from alloy.checkpoints import open_checkpointer
from alloy.engine import Engine, EngineError
from alloy.worktree import WorktreeManager, branch_name
from conftest import (
    bd_create,
    context_entry,
    critic_entry,
    implement_entry,
    judge_entry,
    scope_entry,
    synthesize_entry,
    write_tests_entry,
)
from support import load_config, scope_config


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


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc


def _is_ancestor(ancestor: str, descendant: str, *, cwd: Path) -> bool:
    return _git(cwd, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0


def _porcelain(cwd: Path) -> str:
    return _git(cwd, "status", "--porcelain").stdout.strip()


def _bead_notes(repo: Path, bead_id: str) -> str:
    proc = subprocess.run(
        ["bd", "show", bead_id, "--json"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    if isinstance(payload, list):
        payload = payload[0]
    # `bd note` appends to the issue's `notes` field (see `bd note --help`).
    comments = payload.get("comments") or []
    return "\n".join(
        [
            str(payload.get("notes") or ""),
            *(c.get("body", "") if isinstance(c, dict) else str(c) for c in comments),
        ]
    )


WIP_TEST_PATH = "tests/test_parent_wip_feature.py"
WIP_TEST_CONTENT = "def test_parent_wip_marker():\n    assert True\n"


async def _paused_parent_with_wip(
    engine: Engine,
    beads_project: Path,
    fake_harnesses,
    *,
    extra_wip_writes: list[dict] | None = None,
) -> tuple[str, str, Path, str]:
    """Return parent bead id, parent run id, parent worktree path, base commit (pre-WIP)."""
    parent_id = bd_create(beads_project, "parent feature", alloy_recipe="tdd-loop")
    fake_harnesses.configure(script(judge=[judge_entry("human", "paused for remediation")]))
    parent_result = await engine.run(parent_id)
    assert parent_result.outcome == "waiting-human"

    worktrees = WorktreeManager(repo=beads_project, root=engine.paths.worktrees)
    parent_wt = worktrees.ensure(parent_id)
    base_before_wip = parent_wt.base_commit

    wt_path = Path(parent_result.worktree)
    (wt_path / WIP_TEST_PATH).parent.mkdir(parents=True, exist_ok=True)
    (wt_path / WIP_TEST_PATH).write_text(WIP_TEST_CONTENT, encoding="utf-8")
    for spec in extra_wip_writes or []:
        path = wt_path / spec["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(spec["content"], encoding="utf-8")

    return parent_id, parent_result.run_id, wt_path, base_before_wip


async def _parent_context(engine: Engine, parent_id: str, parent_run_id: str, checkpointer):
    bead = engine.beads.show(parent_id)
    recipe = bead.recipe or "tdd-loop"
    return engine.build_context(
        bead,
        recipe,
        run_id=parent_run_id,
        checkpointer=checkpointer,
    )


def _bug_script(**implement_overrides) -> dict:
    impl = implement_entry(succeed=True)
    impl.update(implement_overrides)
    return script(implement=[impl])


async def test_run_child_wip_commit_child_from_base_merges_fix_and_calls_gate(
    engine,
    beads_project,
    fake_harnesses,
):
    parent_id, parent_run_id, parent_wt, base_before_wip = await _paused_parent_with_wip(
        engine,
        beads_project,
        fake_harnesses,
    )
    bug_id = bd_create(beads_project, "fix pre-existing bug", alloy_recipe="tdd-loop")
    engine.beads.claim(bug_id)

    gate_calls: list[tuple[str, str]] = []

    async def gate_ok(bead, diff: str):
        gate_calls.append((bead.id, diff))
        return True, "ok"

    fake_harnesses.reset_calls()
    fake_harnesses.configure(_bug_script())

    async with open_checkpointer(engine.paths.workflows_db) as checkpointer:
        parent_ctx = await _parent_context(engine, parent_id, parent_run_id, checkpointer)
        child_result = await engine.run_child(
            bug_id,
            parent=parent_ctx,
            merge_gate=gate_ok,
        )

    assert child_result.outcome == "done"

    wip_commits = [line for line in _git(parent_wt, "log", "--oneline", "-5").stdout.splitlines() if bug_id in line]
    assert wip_commits, "expected a WIP commit on the parent branch mentioning the bug id"
    wip_sha = _git(parent_wt, "rev-parse", "HEAD").stdout.strip()

    child_branch = branch_name(bug_id)
    child_tip = _git(beads_project, "rev-parse", child_branch).stdout.strip()
    assert not _is_ancestor(wip_sha, child_tip, cwd=beads_project)
    assert _is_ancestor(base_before_wip, child_tip, cwd=beads_project)

    parent_record = engine.store.get_run(parent_run_id)
    child_record = engine.store.latest_run_for_bead(bug_id)
    assert child_record is not None
    assert child_record["parent_run_id"] == parent_run_id
    assert len(engine.store.all_runs(limit=20, repo=beads_project)) >= 2

    assert "slugify" in (parent_wt / "mypkg" / "__init__.py").read_text(encoding="utf-8")
    assert (parent_wt / WIP_TEST_PATH).is_file()
    assert _porcelain(parent_wt) == ""

    bug = engine.beads.show(bug_id)
    assert bug.status == bd.STATUS_REVIEW_READY
    notes = _bead_notes(beads_project, bug_id)
    assert parent_id in notes

    assert len(gate_calls) == 1
    assert gate_calls[0][0] == bug_id
    assert gate_calls[0][1].strip()


async def test_run_child_guard_rejects_modifying_tests_added_in_parent_wip(
    engine,
    beads_project,
    fake_harnesses,
):
    parent_id, parent_run_id, parent_wt, _ = await _paused_parent_with_wip(
        engine,
        beads_project,
        fake_harnesses,
    )
    bug_id = bd_create(beads_project, "bad fix", alloy_recipe="tdd-loop")
    engine.beads.claim(bug_id)

    async def gate_ok(bead, diff: str):
        return True, "ok"

    fake_harnesses.reset_calls()
    # The child must still land its fix (so its own suite is green and it
    # ends `done`); on top of that it rewrites a test the parent's WIP added.
    fake_harnesses.configure(
        _bug_script(
            write=[
                *implement_entry(succeed=True)["write"],
                {"path": WIP_TEST_PATH, "content": "# child overwrote wip test\n"},
            ],
        )
    )

    async with open_checkpointer(engine.paths.workflows_db) as checkpointer:
        parent_ctx = await _parent_context(engine, parent_id, parent_run_id, checkpointer)
        result = await engine.run_child(bug_id, parent=parent_ctx, merge_gate=gate_ok)

    assert result.outcome == "failed"
    assert WIP_TEST_PATH in result.reason
    assert _porcelain(parent_wt) == ""
    merge_log = _git(parent_wt, "log", "--oneline", "-3").stdout
    assert "Merge" not in merge_log


async def test_run_child_failed_merge_gate_aborts_without_merge(
    engine,
    beads_project,
    fake_harnesses,
):
    parent_id, parent_run_id, parent_wt, _ = await _paused_parent_with_wip(
        engine,
        beads_project,
        fake_harnesses,
    )
    bug_id = bd_create(beads_project, "too broad fix", alloy_recipe="tdd-loop")
    engine.beads.claim(bug_id)

    async def gate_reject(bead, diff: str):
        return False, "too-broad: rewrites the store layer"

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
    assert "too-broad: rewrites the store layer" in result.reason
    assert _porcelain(parent_wt) == ""
    assert "Merge" not in _git(parent_wt, "log", "--oneline", "-3").stdout


async def test_run_child_without_merge_gate_fails_with_explicit_reason(
    engine,
    beads_project,
    fake_harnesses,
):
    parent_id, parent_run_id, parent_wt, _ = await _paused_parent_with_wip(
        engine,
        beads_project,
        fake_harnesses,
    )
    bug_id = bd_create(beads_project, "fix without gate", alloy_recipe="tdd-loop")
    engine.beads.claim(bug_id)

    fake_harnesses.reset_calls()
    fake_harnesses.configure(_bug_script())

    async with open_checkpointer(engine.paths.workflows_db) as checkpointer:
        parent_ctx = await _parent_context(engine, parent_id, parent_run_id, checkpointer)
        result = await engine.run_child(bug_id, parent=parent_ctx, merge_gate=None)

    assert result.outcome == "failed"
    assert "no merge gate bound" in result.reason
    assert _porcelain(parent_wt) == ""


async def test_run_child_merge_conflict_leaves_parent_at_wip_and_keeps_child_branch(
    engine,
    beads_project,
    fake_harnesses,
):
    conflict_path = "mypkg/__init__.py"
    parent_id, parent_run_id, parent_wt, _ = await _paused_parent_with_wip(
        engine,
        beads_project,
        fake_harnesses,
        extra_wip_writes=[{"path": conflict_path, "content": "# parent wip line\n"}],
    )
    bug_id = bd_create(beads_project, "conflicting fix", alloy_recipe="tdd-loop")
    engine.beads.claim(bug_id)

    async def gate_ok(bead, diff: str):
        return True, "ok"

    fake_harnesses.reset_calls()
    # A green fix in the child that still collides with the parent's WIP edit.
    fix = implement_entry(succeed=True)["write"][0]["content"]
    fake_harnesses.configure(
        _bug_script(
            write=[{"path": conflict_path, "content": fix + "# child conflicting line\n"}],
        )
    )

    async with open_checkpointer(engine.paths.workflows_db) as checkpointer:
        parent_ctx = await _parent_context(engine, parent_id, parent_run_id, checkpointer)
        result = await engine.run_child(bug_id, parent=parent_ctx, merge_gate=gate_ok)

    assert result.outcome == "failed"
    assert "merge conflict" in result.reason.lower()
    assert conflict_path in result.reason
    assert _porcelain(parent_wt) == ""
    assert _git(beads_project, "rev-parse", "--verify", branch_name(bug_id), check=False).returncode == 0


async def test_run_child_agent_calls_do_not_roll_up_into_parent_check_limits(
    engine,
    beads_project,
    fake_harnesses,
):
    parent_id, parent_run_id, _, _ = await _paused_parent_with_wip(
        engine,
        beads_project,
        fake_harnesses,
    )
    bug_id = bd_create(beads_project, "counted fix", alloy_recipe="tdd-loop")
    engine.beads.claim(bug_id)

    async def gate_ok(bead, diff: str):
        return True, "ok"

    parent_calls_before = engine.store.call_count(parent_run_id)
    fake_harnesses.reset_calls()
    fake_harnesses.configure(_bug_script())

    async with open_checkpointer(engine.paths.workflows_db) as checkpointer:
        parent_ctx = await _parent_context(engine, parent_id, parent_run_id, checkpointer)
        await engine.run_child(bug_id, parent=parent_ctx, merge_gate=gate_ok)

    child_run = engine.store.latest_run_for_bead(bug_id)
    child_calls = engine.store.call_count(child_run["run_id"])
    assert child_calls >= 1

    total = engine.store.call_count(parent_run_id, include_children=True)
    assert total == parent_calls_before + child_calls

    # The child's calls do not roll up: a cap below the parent+child total
    # but above the parent's own count does not breach.
    recipe = replace(load_config(), limits=replace(load_config().limits, max_agent_calls=total - 1))
    parent_ctx.recipe = recipe
    assert parent_ctx.check_limits({"iteration": 0, "consiliums": 0}) is None

    # The parent's own calls still breach when they reach the cap.
    recipe = replace(load_config(), limits=replace(load_config().limits, max_agent_calls=parent_calls_before))
    parent_ctx.recipe = recipe
    breach = parent_ctx.check_limits({"iteration": 0, "consiliums": 0})
    assert breach is not None
    assert "max_agent_calls reached" in breach


async def test_run_child_scope_merge_proceeds_when_gate_accepts(
    engine,
    beads_project,
    fake_harnesses,
):
    from alloy.recipes.tdd_loop import scope_merge_gate

    parent_id, parent_run_id, parent_wt, _ = await _paused_parent_with_wip(
        engine,
        beads_project,
        fake_harnesses,
    )
    bug_id = bd_create(beads_project, "scoped fix", alloy_recipe="tdd-loop")
    engine.beads.claim(bug_id)

    fake_harnesses.reset_calls()
    fake_harnesses.configure(
        {
            **_bug_script(),
            "scope": scope_entry("merge", "minimal auth fix"),
        }
    )

    async with open_checkpointer(engine.paths.workflows_db) as checkpointer:
        parent_ctx = await _parent_context(engine, parent_id, parent_run_id, checkpointer)
        parent_ctx.recipe = scope_config()

        async def merge_gate(bug, diff: str):
            return await scope_merge_gate(parent_ctx, bug, diff)

        child_result = await engine.run_child(
            bug_id,
            parent=parent_ctx,
            merge_gate=merge_gate,
        )

    assert child_result.outcome == "done"
    assert len(fake_harnesses.calls_for("scope")) == 1
    assert "slugify" in (parent_wt / "mypkg" / "__init__.py").read_text(encoding="utf-8")
    assert _porcelain(parent_wt) == ""
    child_tip = _git(beads_project, "rev-parse", branch_name(bug_id)).stdout.strip()
    parent_head = _git(parent_wt, "rev-parse", "HEAD").stdout.strip()
    assert _is_ancestor(child_tip, parent_head, cwd=parent_wt)


async def test_run_child_scope_too_broad_aborts_without_merge(
    engine,
    beads_project,
    fake_harnesses,
):
    from alloy.recipes.tdd_loop import scope_merge_gate

    parent_id, parent_run_id, parent_wt, _ = await _paused_parent_with_wip(
        engine,
        beads_project,
        fake_harnesses,
    )
    bug_id = bd_create(beads_project, "too broad fix", alloy_recipe="tdd-loop")
    engine.beads.claim(bug_id)

    reason = "rewrites the storage layer"
    fake_harnesses.reset_calls()
    fake_harnesses.configure(
        {
            **_bug_script(),
            "scope": scope_entry("too-broad", reason),
        }
    )

    async with open_checkpointer(engine.paths.workflows_db) as checkpointer:
        parent_ctx = await _parent_context(engine, parent_id, parent_run_id, checkpointer)
        parent_ctx.recipe = scope_config()

        async def merge_gate(bug, diff: str):
            return await scope_merge_gate(parent_ctx, bug, diff)

        result = await engine.run_child(bug_id, parent=parent_ctx, merge_gate=merge_gate)

    assert result.outcome == "failed"
    assert "too-broad" in result.reason
    assert reason in result.reason
    assert _porcelain(parent_wt) == ""
    assert "Merge" not in _git(parent_wt, "log", "--oneline", "-3").stdout


async def test_run_child_scope_runner_missing_fails_without_merge(
    engine,
    beads_project,
    fake_harnesses,
):
    from alloy.recipes.tdd_loop import scope_merge_gate

    parent_id, parent_run_id, parent_wt, _ = await _paused_parent_with_wip(
        engine,
        beads_project,
        fake_harnesses,
    )
    bug_id = bd_create(beads_project, "unscoped fix", alloy_recipe="tdd-loop")
    engine.beads.claim(bug_id)

    fake_harnesses.reset_calls()
    fake_harnesses.configure(_bug_script())

    async with open_checkpointer(engine.paths.workflows_db) as checkpointer:
        parent_ctx = await _parent_context(engine, parent_id, parent_run_id, checkpointer)
        parent_ctx.recipe = scope_config(runner="missing-scope-runner", fallback=None)

        async def merge_gate(bug, diff: str):
            return await scope_merge_gate(parent_ctx, bug, diff)

        result = await engine.run_child(bug_id, parent=parent_ctx, merge_gate=merge_gate)

    assert result.outcome == "failed"
    assert "scope role failed" in result.reason
    assert _porcelain(parent_wt) == ""
    assert "Merge" not in _git(parent_wt, "log", "--oneline", "-3").stdout


async def test_run_child_refuses_when_parent_run_is_already_a_child(
    engine,
    beads_project,
    fake_harnesses,
):
    parent_id, parent_run_id, _, _ = await _paused_parent_with_wip(
        engine,
        beads_project,
        fake_harnesses,
    )
    engine.store.update_run(parent_run_id, parent_run_id="grandparent-run-id")
    bug_id = bd_create(beads_project, "nested fix", alloy_recipe="tdd-loop")
    engine.beads.claim(bug_id)

    async def gate_ok(bead, diff: str):
        return True, "ok"

    fake_harnesses.reset_calls()
    fake_harnesses.configure(_bug_script())

    async with open_checkpointer(engine.paths.workflows_db) as checkpointer:
        parent_ctx = await _parent_context(engine, parent_id, parent_run_id, checkpointer)
        with pytest.raises(EngineError):
            await engine.run_child(bug_id, parent=parent_ctx, merge_gate=gate_ok)

    assert fake_harnesses.calls == []
