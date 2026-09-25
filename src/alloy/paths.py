"""Filesystem layout for project state and shared user configuration.

    <project>/.alloy/
      alloy.db          run + agent-call ledger (Alloy metadata)
      workflows.db      LangGraph checkpoints
      logs/<run_id>/    raw agent transcripts and test output
      worktrees/<bead>/ isolated git checkout per task
      scheduler.pid     daemon lockfile
    ~/.alloy/
      recipes/          user-supplied recipe configs (override built-ins)
      limits.json       last harness usage-limit samples (alloy.limits)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_ROOT = "ALLOY_HOME"
ENV_PROJECT_ROOT = "ALLOY_ROOT"


@dataclass(frozen=True)
class AlloyPaths:
    root: Path

    @classmethod
    def resolve(
        cls, root: Path | str | None = None, *, project: Path | str | None = None,
    ) -> "AlloyPaths":
        if root is None:
            root = os.environ.get(ENV_PROJECT_ROOT) or (
                Path(project or Path.cwd()) / ".alloy"
            )
        return cls(Path(root).expanduser().resolve())

    @property
    def shared_root(self) -> Path:
        root = os.environ.get(ENV_ROOT) or (Path.home() / ".alloy")
        return Path(root).expanduser().resolve()

    @property
    def alloy_db(self) -> Path:
        return self.root / "alloy.db"

    @property
    def workflows_db(self) -> Path:
        return self.root / "workflows.db"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def worktrees(self) -> Path:
        return self.root / "worktrees"

    @property
    def recipes(self) -> Path:
        return self.shared_root / "recipes"

    @property
    def scheduler_pid(self) -> Path:
        return self.root / "scheduler.pid"

    @property
    def scheduler_session(self) -> Path:
        return self.root / "scheduler.json"

    @property
    def scheduler_log(self) -> Path:
        return self.root / "scheduler.log"

    @property
    def limits_cache(self) -> Path:
        return self.shared_root / "limits.json"

    def run_logs(self, run_id: str) -> Path:
        return self.logs / run_id

    def worktree_for(self, bead_id: str) -> Path:
        return self.worktrees / bead_id

    def ensure(self) -> "AlloyPaths":
        for directory in (self.root, self.logs, self.worktrees, self.recipes):
            directory.mkdir(parents=True, exist_ok=True)
        return self


PROJECT_BRIEF_FILE = Path(".alloy") / "project.md"
README_FILE = Path("README.md")
PROJECT_BRIEF_LINES = 60
NO_PROJECT_BRIEF = "(no project brief)"


def project_brief_source(repo: Path | str) -> str | None:
    """Which file the project brief comes from: the operator's, else the README."""
    repo = Path(repo)
    if (repo / PROJECT_BRIEF_FILE).is_file():
        return PROJECT_BRIEF_FILE.as_posix()
    if (repo / README_FILE).is_file():
        return README_FILE.as_posix()
    return None


def project_brief(repo: Path | str, *, lines: int = PROJECT_BRIEF_LINES) -> str:
    """The operator's `.alloy/project.md` in full, else the head of README.md."""
    repo = Path(repo)
    source = project_brief_source(repo)
    if source is None:
        return NO_PROJECT_BRIEF
    try:
        text = (repo / source).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return NO_PROJECT_BRIEF
    if source == PROJECT_BRIEF_FILE.as_posix():
        return text
    return "".join(text.splitlines(keepends=True)[:lines])
