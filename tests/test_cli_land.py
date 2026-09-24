"""Acceptance tests for alloy-vrh.8: Engine.land and `alloy land <id>`.

Behaviour is not implemented yet — these must fail until landing runs the land
recipe, merges into the primary checkout, and surfaces landing metadata on
`alloy status --json`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alloy import beads as bd
from alloy.cli import app
from alloy.engine import Engine
from alloy.worktree import Worktree, WorktreeManager, branch_name
from conftest import acceptance_entry, bd_create, judge_entry, verifier_run_entry, verifier_stop_entry
from test_beads import _create_child, _create_epic

FULL_SUITE = f"{sys.executable} -m pytest -q"


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


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc


def _head(cwd: Path) -> str:
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


def _head_parent_count(cwd: Path) -> int:
    parts = _git(cwd, "rev-list", "--parents", "-n", "1", "HEAD").stdout.strip().split()
    return len(parts) - 1


def _branch_exists(repo: Path, bead_id: str) -> bool:
    proc = _git(
        repo, "show-ref", "--verify", f"refs/heads/{branch_name(bead_id)}", check=False,
    )
    return proc.returncode == 0


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
    """Bead branch with a commit; main advanced separately; returns primary HEAD."""
    (worktree.path / "feature.txt").write_text("bead work\n", encoding="utf-8")
    (worktree.path / "tests").mkdir(exist_ok=True)
    (worktree.path / "tests" / "test_placeholder.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8",
    )
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", "bead commit")

    (project / "main.txt").write_text("main advance\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "main advance")
    return _head(project)


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
    """Engine without tdd-loop load_config override — land must load the real recipe."""
    return Engine.open(beads_project, alloy_home)


# -- happy path --------------------------------------------------------------


def test_cli_land_review_ready_green_verifier_merges_closes_and_reports_landed(
    land_engine, beads_project, alloy_home, fake_harnesses,
):
    """`alloy land B` on review-ready B merges alloy/B into main and closes B."""
    fake_harnesses.configure(_land_script())
    bead_id = bd_create(beads_project, "feature to land", alloy_recipe="tdd-loop")
    worktree = _seed_review_ready(land_engine, beads_project, alloy_home, bead_id)
    primary_head_before = _prepare_clean_merge(beads_project, worktree)
    bead_tip = _head(worktree.path)
    worktree_path = worktree.path

    result = _invoke(
        "land", bead_id, "--json", project=beads_project, alloy_home=alloy_home,
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["bead"] == bead_id
    assert payload["landing"]["state"] == "landed"
    assert payload["landing"]["sha"]

    assert _head(beads_project) != primary_head_before
    assert _head_parent_count(beads_project) == 2
    merge_parents = _git(
        beads_project, "rev-list", "--parents", "-n", "1", "HEAD",
    ).stdout.strip().split()[1:]
    # The merge's second parent is the trial-merge commit the land recipe
    # verified -- it stays on alloy/B (docs/plans/auto-land.md), so the bead's
    # own tip arrives as that commit's parent, not as a direct merge parent.
    trial_merge_parents = _git(
        beads_project, "rev-list", "--parents", "-n", "1", merge_parents[1],
    ).stdout.strip().split()[1:]
    assert bead_tip in trial_merge_parents

    bead = land_engine.beads.show(bead_id)
    assert bead.status == bd.STATUS_DONE
    assert bead.metadata.get("alloy_land_state") == "landed"
    assert bead.metadata.get("alloy_land_sha") == payload["landing"]["sha"]

    assert not worktree_path.exists()
    assert not _branch_exists(beads_project, bead_id)

    status = _invoke("status", bead_id, "--json", project=beads_project, alloy_home=alloy_home)
    assert status.exit_code == 0
    row = _status_bead(json.loads(status.stdout), bead_id)
    assert row["landing"] == {
        "state": "landed",
        "sha": payload["landing"]["sha"],
        "repair": bead.metadata.get("alloy_land_repair"),
    }


@pytest.mark.asyncio
async def test_land_commits_uncommitted_work_before_merging(
    land_engine, beads_project, alloy_home, fake_harnesses,
):
    """Uncommitted agent work must be committed before the land recipe runs."""
    fake_harnesses.configure(_land_script())
    bead_id = bd_create(beads_project, "dirty worktree", alloy_recipe="tdd-loop")
    worktree = _seed_review_ready(land_engine, beads_project, alloy_home, bead_id)
    (worktree.path / "feature.txt").write_text("bead work\n", encoding="utf-8")
    (worktree.path / "tests").mkdir(exist_ok=True)
    (worktree.path / "tests" / "test_placeholder.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8",
    )
    # Deliberately uncommitted: land must not verify/merge HEAD without this file.
    (worktree.path / "only_dirty.txt").write_text("wip\n", encoding="utf-8")
    primary_head_before = _head(beads_project)
    (beads_project / "main.txt").write_text("main advance\n", encoding="utf-8")
    _git(beads_project, "add", "-A")
    _git(beads_project, "commit", "-m", "main advance")

    await land_engine.land(bead_id)

    merged = _git(beads_project, "show", "HEAD:only_dirty.txt").stdout
    assert merged.strip() == "wip"
    assert _head(beads_project) != primary_head_before
    assert land_engine.beads.show(bead_id).status == bd.STATUS_DONE


# -- parked when primary checkout is wrong -----------------------------------


def test_cli_land_wrong_primary_branch_parks_bead_and_leaves_main_unchanged(
    land_engine, beads_project, alloy_home, fake_harnesses,
):
    """Primary on another branch: non-zero exit, B waiting-human, main untouched."""
    fake_harnesses.configure(_land_script())
    bead_id = bd_create(beads_project, "park when wrong branch", alloy_recipe="tdd-loop")
    worktree = _seed_review_ready(land_engine, beads_project, alloy_home, bead_id)
    _prepare_clean_merge(beads_project, worktree)

    _git(beads_project, "checkout", "-b", "other-branch")
    main_head_before = _head(beads_project)

    result = _invoke("land", bead_id, project=beads_project, alloy_home=alloy_home)

    assert result.exit_code != 0
    assert "main" in (result.stdout + result.stderr).lower()

    bead = land_engine.beads.show(bead_id)
    assert bead.status == bd.STATUS_WAITING_HUMAN
    assert bead.metadata.get("alloy_land_state") == "parked"
    assert _head(beads_project) == main_head_before

    status = _invoke("status", bead_id, "--json", project=beads_project, alloy_home=alloy_home)
    assert status.exit_code == 0
    row = _status_bead(json.loads(status.stdout), bead_id)
    assert row["landing"]["state"] == "parked"
    assert row["landing"]["sha"] in (None, "")


# -- validation refusals -----------------------------------------------------


def test_cli_land_epic_with_open_child_exits_nonzero_naming_child(
    land_engine, beads_project, alloy_home,
):
    """`alloy land E` refuses when the epic still has an open descendant."""
    epic_id = _create_epic(land_engine.beads, "Ship feature", "Epic for landing")
    child_id = _create_child(land_engine.beads, "unfinished child", epic_id)
    land_engine.beads.claim(epic_id)
    land_engine.beads.set_status(epic_id, bd.STATUS_REVIEW_READY)

    result = _invoke("land", epic_id, project=beads_project, alloy_home=alloy_home)

    assert result.exit_code != 0
    output = result.stdout + result.stderr
    assert child_id in output


def test_cli_land_open_bead_is_refused(land_engine, beads_project, alloy_home):
    """Landing a bead still at STATUS_READY (`open`) is refused."""
    bead_id = bd_create(beads_project, "still open", alloy_recipe="tdd-loop")
    assert land_engine.beads.show(bead_id).status == bd.STATUS_READY

    result = _invoke("land", bead_id, project=beads_project, alloy_home=alloy_home)

    assert result.exit_code != 0
    assert "review-ready" in (result.stdout + result.stderr).lower()
