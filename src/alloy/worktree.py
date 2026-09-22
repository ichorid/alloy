"""One git worktree per task.

Isolation is structural, not advisory: each bead gets its own checkout on its own
branch, so two tasks cannot mutate the same files. Worktrees survive failure so a
human can inspect them, and are only removed on request.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

BRANCH_PREFIX = "alloy"


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Worktree:
    bead_id: str
    path: Path
    branch: str
    base_commit: str

    def exists(self) -> bool:
        return (self.path / ".git").exists()


@dataclass(frozen=True)
class MergeResult:
    ok: bool
    conflict_files: list[str]


def branch_name(bead_id: str) -> str:
    return f"{BRANCH_PREFIX}/{bead_id}"


def _git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=120
    )
    if check and proc.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


@dataclass
class WorktreeManager:
    repo: Path
    root: Path

    def __post_init__(self) -> None:
        self.repo = Path(self.repo).resolve()
        self.root = Path(self.root).resolve()

    def path_for(self, bead_id: str) -> Path:
        return self.root / bead_id

    def ensure(self, bead_id: str, *, base: str = "HEAD") -> Worktree:
        """Create the worktree, or adopt the existing one when resuming."""
        path = self.path_for(bead_id)
        branch = branch_name(bead_id)

        if path.exists() and (path / ".git").exists():
            self._assert_owned(path, branch)
            return Worktree(bead_id, path, branch, self._base_of(path, base))

        if path.exists() and any(path.iterdir()):
            raise WorktreeError(f"{path} exists but is not an Alloy worktree")

        self.root.mkdir(parents=True, exist_ok=True)
        base_commit = _git(["rev-parse", base], self.repo).stdout.strip()

        if self._branch_exists(branch):
            _git(["worktree", "add", str(path), branch], self.repo)
        else:
            _git(["worktree", "add", "-b", branch, str(path), base_commit], self.repo)
        return Worktree(bead_id, path, branch, base_commit)

    def ensure_from(self, bead_id: str, base_commit: str) -> Worktree:
        """A worktree cut from a specific commit rather than the repo's HEAD --
        a remediation child starts from its parent's base, not its parent's work."""
        return self.ensure(bead_id, base=base_commit)

    def remove(self, bead_id: str, *, force: bool = False, delete_branch: bool = False) -> bool:
        path = self.path_for(bead_id)
        if not path.exists():
            return False
        args = ["worktree", "remove", str(path)]
        if force:
            args.append("--force")
        proc = _git(args, self.repo, check=False)
        if proc.returncode != 0:
            return False
        if delete_branch:
            _git(["branch", "-D", branch_name(bead_id)], self.repo, check=False)
        return True

    def prune(self) -> None:
        _git(["worktree", "prune"], self.repo, check=False)

    # -- inspection -------------------------------------------------------

    def diff(self, worktree: Worktree, *, stat_only: bool = False) -> str:
        """Everything the agents changed since the worktree was cut."""
        _git(["add", "-A", "--intent-to-add"], worktree.path, check=False)
        args = ["diff", worktree.base_commit]
        if stat_only:
            args.append("--stat")
        proc = _git(args, worktree.path, check=False)
        return proc.stdout

    def changed_files(self, worktree: Worktree) -> list[str]:
        _git(["add", "-A", "--intent-to-add"], worktree.path, check=False)
        proc = _git(["diff", "--name-only", worktree.base_commit], worktree.path, check=False)
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def has_changes(self, worktree: Worktree) -> bool:
        return bool(self.changed_files(worktree))

    def changed_paths(
        self, worktree: Worktree, since_commit: str, *,
        until: str | None = None, paths: list[str] | None = None,
    ) -> list[str]:
        """Paths that differ between `since_commit` and the working tree (or
        `until`), optionally restricted to `paths`."""
        args = ["diff", "--name-only", since_commit]
        if until:
            args.append(until)
        if paths:
            args += ["--", *paths]
        proc = _git(args, worktree.path, check=False)
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def added_paths(self, commit: str) -> list[str]:
        """Paths the commit introduced (relative to its parent)."""
        proc = _git(
            ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root",
             "--diff-filter=A", commit],
            self.repo, check=False,
        )
        return [line for line in proc.stdout.splitlines() if line.strip()]

    # -- commits and merges -----------------------------------------------

    def commit_wip(self, worktree: Worktree, message: str) -> str | None:
        """Commit everything in the working tree; None when there is nothing to commit."""
        _git(["add", "-A"], worktree.path)
        if _git(["diff", "--cached", "--quiet"], worktree.path, check=False).returncode == 0:
            return None
        _git(["commit", "-q", "--no-verify", "-m", message], worktree.path)
        return self._head(worktree.path)

    def merge_branch(self, worktree: Worktree, branch: str) -> MergeResult:
        """Merge `branch` into the worktree's branch; on conflict, abort and
        leave the tree exactly where it was."""
        proc = _git(["merge", "--no-edit", branch], worktree.path, check=False)
        if proc.returncode == 0:
            return MergeResult(True, [])
        conflicts = _git(["diff", "--name-only", "--diff-filter=U"], worktree.path, check=False)
        _git(["merge", "--abort"], worktree.path, check=False)
        files = [line for line in conflicts.stdout.splitlines() if line.strip()]
        return MergeResult(False, files)

    # -- internals --------------------------------------------------------

    def _head(self, path: Path) -> str:
        proc = _git(["rev-parse", "HEAD"], path, check=False)
        return proc.stdout.strip()

    def _base_of(self, path: Path, base: str) -> str:
        """Where the task's changes start when adopting an existing worktree.

        Not the worktree's HEAD: if the operator merged `main` forward into
        the branch, or an agent committed, HEAD has moved and a diff against
        it would hide work the judge must see. The merge-base with the
        repository's `base` (HEAD by default) is the last commit both sides
        share, so the diff is exactly what this task added.
        """
        repo_base = _git(["rev-parse", base], self.repo, check=False).stdout.strip()
        head = self._head(path)
        if not repo_base or not head:
            return head
        proc = _git(["merge-base", head, repo_base], path, check=False)
        return proc.stdout.strip() or head

    def _branch_exists(self, branch: str) -> bool:
        proc = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                    self.repo, check=False)
        return proc.returncode == 0

    def _assert_owned(self, path: Path, branch: str) -> None:
        proc = _git(["rev-parse", "--abbrev-ref", "HEAD"], path, check=False)
        current = proc.stdout.strip()
        if current and current != branch:
            raise WorktreeError(
                f"worktree {path} is on branch '{current}', expected '{branch}'; "
                "refusing to let two tasks share a checkout"
            )
