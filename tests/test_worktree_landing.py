"""Landing primitives on WorktreeManager (alloy-vrh.2)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from alloy.worktree import LandResult, WorktreeManager, branch_name


@pytest.fixture
def manager(project, tmp_path):
    return WorktreeManager(repo=project, root=tmp_path / "worktrees")


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


def _head_parent_count(cwd: Path) -> int:
    parts = _git(cwd, "rev-list", "--parents", "-n", "1", "HEAD").stdout.strip().split()
    return len(parts) - 1


def _porcelain(cwd: Path) -> str:
    return _git(cwd, "status", "--porcelain").stdout.strip()


def test_trial_merge_non_conflicting_target_returns_ok_with_two_parent_head(manager, project):
    worktree = manager.ensure("bd-1")
    (worktree.path / "feature.txt").write_text("bead work\n", encoding="utf-8")
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", "bead commit")

    (project / "main.txt").write_text("main advance\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "main advance")

    result = manager.trial_merge(worktree, "main")

    assert result.ok is True
    assert result.conflict_files == []
    assert _head_parent_count(worktree.path) == 2


def test_trial_merge_conflict_lists_files_and_leaves_worktree_unchanged(manager, project):
    worktree = manager.ensure("bd-1")
    conflict_path = "mypkg/__init__.py"
    (worktree.path / conflict_path).write_text("bead = 1\n", encoding="utf-8")
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", "bead change")

    (project / conflict_path).write_text("main = 2\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "main change")

    head_before = _head(worktree.path)
    status_before = _porcelain(worktree.path)

    result = manager.trial_merge(worktree, "main")

    assert result.ok is False
    assert conflict_path in result.conflict_files
    assert _head(worktree.path) == head_before
    assert _porcelain(worktree.path) == status_before


def test_merge_into_primary_on_target_returns_ok_and_no_ff_merge_sha(manager, project):
    worktree = manager.ensure("bd-1")
    (worktree.path / "landed.txt").write_text("landed feature\n", encoding="utf-8")
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", "ready to land")

    result = manager.merge_into_primary(branch_name("bd-1"), "main")

    assert isinstance(result, LandResult)
    assert result.ok is True
    assert result.sha == _head(project)
    assert result.reason == ""
    assert _head_parent_count(project) == 2


def test_merge_into_primary_wrong_branch_returns_false_with_branch_in_reason(manager, project):
    worktree = manager.ensure("bd-1")
    (worktree.path / "landed.txt").write_text("landed feature\n", encoding="utf-8")
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", "ready to land")

    _git(project, "checkout", "-b", "other-branch")
    head_before = _head(project)

    result = manager.merge_into_primary(branch_name("bd-1"), "main")

    assert result.ok is False
    assert "main" in result.reason
    assert result.sha == ""
    assert _head(project) == head_before


def test_merge_into_primary_dirty_primary_preserves_uncommitted_bytes(manager, project):
    worktree = manager.ensure("bd-1")
    readme = "README.md"
    (worktree.path / readme).write_text("# bead version\n", encoding="utf-8")
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", "bead readme")

    head_before = _head(project)
    dirty_bytes = b"# dirty uncommitted edit\n"
    (project / readme).write_bytes(dirty_bytes)

    result = manager.merge_into_primary(branch_name("bd-1"), "main")

    assert result.ok is False
    assert (project / readme).read_bytes() == dirty_bytes
    assert _head(project) == head_before
