"""Worktree isolation: two tasks can never share a checkout."""

from __future__ import annotations

import subprocess

import pytest

from alloy.worktree import WorktreeError, WorktreeManager, branch_name


@pytest.fixture
def manager(project, tmp_path):
    return WorktreeManager(repo=project, root=tmp_path / "worktrees")


def test_each_bead_gets_its_own_checkout_and_branch(manager):
    first = manager.ensure("bd-1")
    second = manager.ensure("bd-2")

    assert first.path != second.path
    assert first.branch == branch_name("bd-1")
    assert second.branch == branch_name("bd-2")
    assert first.path.is_dir() and second.path.is_dir()


def test_edits_in_one_worktree_are_invisible_in_the_other(manager):
    first = manager.ensure("bd-1")
    second = manager.ensure("bd-2")

    (first.path / "mypkg" / "__init__.py").write_text("X = 1\n", encoding="utf-8")

    assert (second.path / "mypkg" / "__init__.py").read_text() == ""
    assert manager.changed_files(first) == ["mypkg/__init__.py"]
    assert manager.changed_files(second) == []


def test_source_repository_is_untouched(manager, project):
    worktree = manager.ensure("bd-1")
    (worktree.path / "mypkg" / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    assert (project / "mypkg" / "__init__.py").read_text() == ""


def test_ensure_is_idempotent_so_resume_adopts_the_existing_worktree(manager):
    first = manager.ensure("bd-1")
    (first.path / "new.txt").write_text("kept", encoding="utf-8")

    again = manager.ensure("bd-1")

    assert again.path == first.path
    assert (again.path / "new.txt").read_text() == "kept"


def test_refuses_a_checkout_that_belongs_to_another_task(manager):
    worktree = manager.ensure("bd-1")
    subprocess.run(["git", "checkout", "-q", "-b", "someone-else"], cwd=worktree.path, check=True, capture_output=True)
    with pytest.raises(WorktreeError, match="two tasks share a checkout"):
        manager.ensure("bd-1")


def test_diff_is_relative_to_the_commit_the_worktree_was_cut_from(manager):
    worktree = manager.ensure("bd-1")
    (worktree.path / "mypkg" / "__init__.py").write_text("def slugify(s):\n    ...\n", encoding="utf-8")
    diff = manager.diff(worktree)
    assert "def slugify" in diff
    assert manager.has_changes(worktree)


def test_adopting_a_worktree_keeps_the_task_diff_after_commits_on_the_branch(manager):
    """A merge from main (or an agent that commits) moves HEAD; the judge
    must still see everything the task changed since it branched off."""
    worktree = manager.ensure("bd-1")
    (worktree.path / "mypkg" / "__init__.py").write_text("def slugify(s):\n    ...\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "agent committed"], cwd=worktree.path, check=True, capture_output=True)
    (worktree.path / "mypkg" / "extra.py").write_text("MORE = 1\n", encoding="utf-8")

    adopted = manager.ensure("bd-1")

    assert adopted.base_commit == worktree.base_commit
    assert set(manager.changed_files(adopted)) == {"mypkg/__init__.py", "mypkg/extra.py"}


def test_removal_is_explicit_so_failures_stay_inspectable(manager):
    worktree = manager.ensure("bd-1")
    (worktree.path / "scratch.txt").write_text("evidence", encoding="utf-8")

    assert manager.remove("bd-1", force=True)
    assert not worktree.path.exists()
    assert not manager.remove("bd-1")
