"""Inspecting and committing in the one primary checkout.

Every bead runs directly in the primary checkout, on whatever branch is
already checked out there -- no isolated worktree, no separate bead branch,
no merge. What is left here is what a run still needs: what changed since its
own base commit, and committing its work.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

log = logging.getLogger("alloy.worktree")

JUNK_PATTERNS: tuple[str, ...] = (
    "uv.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "poetry.lock",
    "*.pyc",
    "__pycache__/",
    ".serena/",
    ".DS_Store",
    "*.egg-info/",
)
"""Generated files that must never reach a diff the agents read (journal 20, 33).

Glob-style, gitignore-like: a trailing slash names a directory. Untracked junk
enters the index via `git add -A --intent-to-add`, so it is excluded by pathspec
at diff time rather than relying on .gitignore.
"""


def junk_pathspecs(patterns: tuple[str, ...] = JUNK_PATTERNS) -> list[str]:
    """`:(exclude)` pathspecs matching each junk pattern at any depth.

    Glob magic is needed: a plain `:(exclude)uv.lock` only matches the top level
    and `:(exclude)*.egg-info/` does not match at all.
    """
    specs = []
    for pattern in patterns:
        if pattern.endswith("/"):
            specs.append(f":(exclude,glob)**/{pattern}**")
        else:
            specs.append(f":(exclude,glob)**/{pattern}")
    return specs


def is_test_path(path: str) -> bool:
    """A path that looks like a test by location or name (tests/, test_*.py, *_test.py)."""
    parts = PurePosixPath(path)
    name = parts.name
    return (
        any(part in ("tests", "test") for part in parts.parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


class WorktreeError(RuntimeError):
    pass


FINGERPRINT_EXCLUDES: tuple[str, ...] = (".alloy/", ".beads/")
"""Alloy's own state and the bead database change while a run works without
changing the code under test; they never perturb `tree_fingerprint`."""

_FINGERPRINT_MAX_HASHED_BYTES = 4 * 1024 * 1024


def tree_fingerprint(path: Path) -> str:
    """A hash of everything a check could see: HEAD, `git diff HEAD` (tracked
    edits, including intent-to-add files) and every untracked, not-ignored
    file's contents (size + mtime for very large files). Junk files, `.alloy/`
    and `.beads/` are excluded. "" when `path` is not a git checkout -- an
    unknown tree never matches anything.

    Read-only: unlike `WorktreeManager.diff` it never touches the index."""
    excludes = [*junk_pathspecs(), *junk_pathspecs(FINGERPRINT_EXCLUDES)]
    try:
        head = _git(["rev-parse", "HEAD"], path, check=False)
        if head.returncode != 0:
            return ""
        digest = hashlib.sha256(head.stdout.strip().encode())
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--binary", "--", ".", *excludes],
            cwd=str(path),
            capture_output=True,
            timeout=120,
        )
        if diff.returncode != 0:
            return ""
        digest.update(diff.stdout)
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", ".", *excludes],
            cwd=str(path),
            capture_output=True,
            timeout=120,
        )
        if untracked.returncode != 0:
            return ""
    except (OSError, subprocess.SubprocessError):
        return ""
    for name in sorted(item for item in untracked.stdout.decode("utf-8", "surrogateescape").split("\0") if item):
        digest.update(b"\0untracked\0" + name.encode("utf-8", "surrogateescape") + b"\0")
        target = Path(path) / name
        try:
            stat = target.stat()
            if stat.st_size > _FINGERPRINT_MAX_HASHED_BYTES:
                digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
            else:
                digest.update(target.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


@dataclass(frozen=True)
class Worktree:
    bead_id: str
    path: Path
    branch: str
    base_commit: str

    def exists(self) -> bool:
        return (self.path / ".git").exists()


def _git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=120)
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

    def stop_processes(self, path: Path) -> list[int]:
        """No-op: every run works in the primary checkout now, which the
        operator may be using alongside Alloy -- never kill processes there.
        Kept as a call target so callers do not need their own guard."""
        return []

    # -- inspection -------------------------------------------------------

    def diff(self, worktree: Worktree, *, stat_only: bool = False) -> str:
        """Everything the agents changed since the worktree was cut."""
        _git(["add", "-A", "--intent-to-add"], worktree.path, check=False)
        args = ["diff", worktree.base_commit]
        if stat_only:
            args.append("--stat")
        args += ["--", ".", *junk_pathspecs()]
        proc = _git(args, worktree.path, check=False)
        return proc.stdout

    def changed_files(self, worktree: Worktree) -> list[str]:
        _git(["add", "-A", "--intent-to-add"], worktree.path, check=False)
        proc = _git(
            ["diff", "--name-only", worktree.base_commit, "--", ".", *junk_pathspecs()],
            worktree.path,
            check=False,
        )
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def has_changes(self, worktree: Worktree) -> bool:
        return bool(self.changed_files(worktree))

    def is_dirty(self, worktree: Worktree) -> bool:
        """True when the worktree has uncommitted git changes."""
        proc = _git(["status", "--porcelain"], worktree.path, check=False)
        return bool(proc.stdout.strip())

    def has_uncommitted_tracked_changes(self, path: Path) -> bool:
        """True when a *tracked* file is modified/staged/deleted -- ignores
        untracked clutter (e.g. `.beads/`, build artifacts) that a checkout
        commonly carries even when nobody's work is in progress there."""
        proc = _git(["status", "--porcelain", "--untracked-files=no"], path, check=False)
        return bool(proc.stdout.strip())

    def head(self, path: Path) -> str:
        """The worktree's current HEAD commit."""
        return self._head(path)

    def is_ancestor(self, commit: str, of: str) -> bool:
        return _git(["merge-base", "--is-ancestor", commit, of], self.repo, check=False).returncode == 0

    def current_branch(self, path: Path | None = None) -> str:
        """The branch checked out at `path` (the primary checkout by default)."""
        proc = _git(["rev-parse", "--abbrev-ref", "HEAD"], path or self.repo, check=False)
        return proc.stdout.strip()

    def fingerprints(self, worktree: Worktree, paths: list[str]) -> dict[str, str]:
        """sha256 of each path's current contents; paths absent on disk are omitted."""
        result: dict[str, str] = {}
        for path in paths:
            target = worktree.path / path
            if target.is_file():
                result[path] = hashlib.sha256(target.read_bytes()).hexdigest()
        return result

    # -- commits ------------------------------------------------------------

    def commit_wip(self, worktree: Worktree, message: str) -> str | None:
        """Commit everything in the working tree; None when there is nothing to commit."""
        _git(["add", "-A"], worktree.path)
        if _git(["diff", "--cached", "--quiet"], worktree.path, check=False).returncode == 0:
            return None
        _git(["commit", "-q", "--no-verify", "-m", message], worktree.path)
        return self._head(worktree.path)

    # -- internals --------------------------------------------------------

    def _head(self, path: Path) -> str:
        proc = _git(["rev-parse", "HEAD"], path, check=False)
        return proc.stdout.strip()
