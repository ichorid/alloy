"""Acceptance tests for alloy-vrh.9: land failures file remediation bugs.

Behaviour is not implemented yet — these must fail until Engine.land files a
repair bug on conflict/red, sets landing metadata on the landed bead, and
dedupes while a repair bug stays open. Spec: docs/plans/auto-land.md §Failures
become remediation beads.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import (
    acceptance_entry,
    bd_create,
    judge_entry,
    verifier_run_entry,
    verifier_stop_entry,
)
from typer.testing import CliRunner

from alloy import beads as bd
from alloy.beads import BeadsClient
from alloy.cli import app
from alloy.engine import Engine
from alloy.worktree import Worktree, WorktreeManager, branch_name

FULL_SUITE = f"{sys.executable} -m pytest -q"
FAILING_CHECK = 'sh -c "exit 1"'


def _invoke(*args: str, project: Path, alloy_home: Path):
    runner = CliRunner()
    return runner.invoke(
        app,
        [*args, "--repo", str(project), "--root", str(alloy_home)],
    )


def _status_bead(payload: dict, bead_id: str) -> dict:
    for row in payload["beads"]:
        if row["bead"] == bead_id:
            return row
    raise AssertionError(f"bead {bead_id} not found in status payload")


def _show_json(client: BeadsClient, bead_id: str) -> dict:
    rows = client._json(["show", bead_id])
    assert rows, f"bead {bead_id} not found"
    return rows[0]


def _bug_ids(client: BeadsClient) -> list[str]:
    return [row["id"] for row in client._json(["list", "--type", "bug", "--limit", "0", "--flat"])]


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc


def _head(cwd: Path) -> str:
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


def _land_script(**overrides):
    base = {
        "verifier": [
            verifier_run_entry(FULL_SUITE, kind="regression"),
            verifier_stop_entry("regression suite green on merged tree"),
        ],
        "acceptance": [acceptance_entry("accept", confidence=0.9)],
        "judge": [judge_entry("done")],
    }
    base.update(overrides)
    return base


def _prepare_clean_merge(project: Path, worktree: Worktree) -> str:
    (worktree.path / "feature.txt").write_text("bead work\n", encoding="utf-8")
    (worktree.path / "tests").mkdir(exist_ok=True)
    (worktree.path / "tests" / "test_placeholder.py").write_text(
        "def test_placeholder():\n    assert True\n",
        encoding="utf-8",
    )
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", "bead commit")

    (project / "main.txt").write_text("main advance\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "main advance")
    return _head(project)


def _prepare_merge_conflict(
    project: Path,
    worktree: Worktree,
    conflict_path: str = "mypkg/__init__.py",
) -> str:
    (worktree.path / conflict_path).write_text("bead = 1\n", encoding="utf-8")
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", "bead change")

    (project / conflict_path).write_text("main = 2\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "main change")
    return _head(worktree.path)


def _seed_review_ready(
    engine: Engine,
    beads_project: Path,
    alloy_home: Path,
    bead_id: str,
) -> Worktree:
    engine.beads.claim(bead_id)
    engine.beads.set_status(bead_id, bd.STATUS_REVIEW_READY)
    worktrees = WorktreeManager(repo=beads_project, root=alloy_home / "worktrees")
    worktree = worktrees.ensure(bead_id)
    engine.beads.set_metadata(
        bead_id,
        {
            bd.META_WORKTREE: str(worktree.path),
            bd.META_BRANCH: branch_name(bead_id),
        },
    )
    return worktree


@pytest.fixture
def land_engine(beads_project, alloy_home):
    return Engine.open(beads_project, alloy_home)


def _assert_blocks_edge(client: BeadsClient, landed_id: str, bug_id: str) -> None:
    deps = _show_json(client, landed_id).get("dependencies") or []
    assert any(dep.get("id") == bug_id and dep.get("dependency_type") == "blocks" for dep in deps), (
        f"expected {landed_id} blocked by {bug_id}, got {deps}"
    )


def _assert_discovered_from(client: BeadsClient, bug_id: str, landed_id: str) -> None:
    deps = _show_json(client, bug_id).get("dependencies") or []
    assert any(dep.get("id") == landed_id and dep.get("dependency_type") == "discovered-from" for dep in deps), (
        f"expected {bug_id} discovered-from {landed_id}, got {deps}"
    )


# -- conflict files exactly one repair bug -----------------------------------


def test_cli_land_conflict_files_one_repair_bug_blocking_landed_bead(
    land_engine,
    beads_project,
    alloy_home,
    fake_harnesses,
):
    """Conflict on B files one bug with worktree_owner=B, blocks B, repairing state."""
    conflict_path = "mypkg/__init__.py"
    fake_harnesses.configure(_land_script())
    bead_id = bd_create(beads_project, "land conflict repair", alloy_recipe="tdd-loop", alloy_use_worktree="true")
    worktree = _seed_review_ready(land_engine, beads_project, alloy_home, bead_id)
    bead_head_before = _prepare_merge_conflict(beads_project, worktree, conflict_path)
    bugs_before = set(_bug_ids(land_engine.beads))

    result = _invoke("land", bead_id, project=beads_project, alloy_home=alloy_home)

    assert result.exit_code != 0
    assert _head(worktree.path) == bead_head_before

    bugs_after = set(_bug_ids(land_engine.beads))
    new_bugs = bugs_after - bugs_before
    assert len(new_bugs) == 1
    bug_id = next(iter(new_bugs))

    bug = land_engine.beads.show(bug_id)
    assert bug.issue_type == "bug"
    assert bug.metadata.get(bd.META_WORKTREE_OWNER) == bead_id
    assert bug.metadata.get(bd.META_RECIPE) == "tdd-loop"
    assert conflict_path in (bug.acceptance_criteria or "")
    assert bd.LABEL_BUG in bug.labels

    _assert_discovered_from(land_engine.beads, bug_id, bead_id)
    _assert_blocks_edge(land_engine.beads, bead_id, bug_id)
    assert bead_id not in [row.id for row in land_engine.beads.ready()]

    landed = land_engine.beads.show(bead_id)
    assert landed.status == bd.STATUS_REVIEW_READY
    assert landed.metadata.get(bd.META_LAND_STATE) == "repairing"
    assert landed.metadata.get(bd.META_LAND_REPAIR) == bug_id

    status = _invoke("status", bead_id, "--json", project=beads_project, alloy_home=alloy_home)
    assert status.exit_code == 0
    row = _status_bead(json.loads(status.stdout), bead_id)
    assert row["landing"]["state"] == "repairing"
    assert row["landing"]["repair"] == bug_id


def test_cli_land_conflict_retry_does_not_file_second_repair_bug(
    land_engine,
    beads_project,
    alloy_home,
    fake_harnesses,
):
    """Landing B again while its repair bug is open creates no second bug."""
    conflict_path = "mypkg/__init__.py"
    fake_harnesses.configure(_land_script())
    bead_id = bd_create(beads_project, "land conflict dedupe", alloy_recipe="tdd-loop", alloy_use_worktree="true")
    worktree = _seed_review_ready(land_engine, beads_project, alloy_home, bead_id)
    _prepare_merge_conflict(beads_project, worktree, conflict_path)

    first = _invoke("land", bead_id, project=beads_project, alloy_home=alloy_home)
    assert first.exit_code != 0
    bugs_after_first = set(_bug_ids(land_engine.beads))
    assert len(bugs_after_first) == 1

    second = _invoke("land", bead_id, project=beads_project, alloy_home=alloy_home)
    assert second.exit_code != 0
    bugs_after_second = set(_bug_ids(land_engine.beads))
    assert bugs_after_second == bugs_after_first

    landed = land_engine.beads.show(bead_id)
    assert landed.metadata.get(bd.META_LAND_STATE) == "repairing"
    assert landed.metadata.get(bd.META_LAND_REPAIR) in bugs_after_first


# -- red check files repair bug naming the command ---------------------------


def test_cli_land_red_files_repair_bug_acceptance_names_failing_check(
    land_engine,
    beads_project,
    alloy_home,
    fake_harnesses,
):
    """Red post-merge checks file a bug whose acceptance names the failing command."""
    fake_harnesses.configure(
        _land_script(
            verifier=[
                verifier_run_entry(FAILING_CHECK, kind="regression"),
                verifier_stop_entry("should not reach stop after red check"),
            ],
        )
    )
    bead_id = bd_create(beads_project, "land red repair", alloy_recipe="tdd-loop")
    worktree = _seed_review_ready(land_engine, beads_project, alloy_home, bead_id)
    primary_head_before = _prepare_clean_merge(beads_project, worktree)
    bugs_before = set(_bug_ids(land_engine.beads))

    result = _invoke("land", bead_id, project=beads_project, alloy_home=alloy_home)

    assert result.exit_code != 0
    assert _head(beads_project) == primary_head_before

    bugs_after = set(_bug_ids(land_engine.beads))
    new_bugs = bugs_after - bugs_before
    assert len(new_bugs) == 1
    bug_id = next(iter(new_bugs))

    bug = land_engine.beads.show(bug_id)
    assert bug.metadata.get(bd.META_WORKTREE_OWNER) == bead_id
    assert FAILING_CHECK in (bug.acceptance_criteria or "")
    assert bug.metadata.get(bd.META_RECIPE) == "tdd-loop"

    _assert_discovered_from(land_engine.beads, bug_id, bead_id)
    _assert_blocks_edge(land_engine.beads, bead_id, bug_id)

    landed = land_engine.beads.show(bead_id)
    assert landed.metadata.get(bd.META_LAND_STATE) == "repairing"
    assert landed.metadata.get(bd.META_LAND_REPAIR) == bug_id
