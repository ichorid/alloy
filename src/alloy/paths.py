"""Filesystem layout for Alloy's local state.

Everything Alloy owns lives under one root so that a crash, a reboot or a
manual inspection all start from the same place::

    ~/.alloy/
      alloy.db          run + agent-call ledger (Alloy metadata)
      workflows.db      LangGraph checkpoints
      logs/<run_id>/    raw agent transcripts and test output
      worktrees/<bead>/ isolated git checkout per task
      recipes/          user-supplied recipe configs (override built-ins)
      scheduler.pid     daemon lockfile
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_ROOT = "ALLOY_HOME"


@dataclass(frozen=True)
class AlloyPaths:
    root: Path

    @classmethod
    def resolve(cls, root: Path | str | None = None) -> "AlloyPaths":
        if root is None:
            root = os.environ.get(ENV_ROOT) or (Path.home() / ".alloy")
        return cls(Path(root).expanduser().resolve())

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
        return self.root / "recipes"

    @property
    def scheduler_pid(self) -> Path:
        return self.root / "scheduler.pid"

    def run_logs(self, run_id: str) -> Path:
        return self.logs / run_id

    def worktree_for(self, bead_id: str) -> Path:
        return self.worktrees / bead_id

    def ensure(self) -> "AlloyPaths":
        for directory in (self.root, self.logs, self.worktrees, self.recipes):
            directory.mkdir(parents=True, exist_ok=True)
        return self
