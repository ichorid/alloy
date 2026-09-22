"""Beads is the durable project graph and the source of truth for executable work.

Alloy reads readiness and writes execution status back; it never keeps a second
copy of the task list. Per-task workflow detail belongs in LangGraph, not here --
what lands on the bead is status plus a handful of pointers (run id, worktree,
branch, stage).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from alloy.models import COMPLEXITY_LEVELS, Complexity

BD_BINARY = "bd"

# Lifecycle Alloy drives the bead through. `open` is Beads' own "ready".
STATUS_READY = "open"
STATUS_IMPLEMENTING = "implementing"
STATUS_WAITING_HUMAN = "waiting-human"
STATUS_REVIEW_READY = "review-ready"
STATUS_FAILED = "failed"
STATUS_DONE = "closed"

CUSTOM_STATUSES = "implementing:wip,waiting-human:frozen,review-ready:active,failed:active"

# Metadata keys Alloy owns on a bead. Namespaced so humans and other tools can
# tell at a glance what is Alloy's.
META_RECIPE = "alloy_recipe"
META_RUN_ID = "alloy_run_id"
META_WORKTREE = "alloy_worktree"
META_BRANCH = "alloy_branch"
META_STAGE = "alloy_stage"
META_TEST_CMD = "alloy_test_cmd"
META_COMPLEXITY = "alloy_complexity"
META_COMPLEXITY_ESTIMATED = "alloy_complexity_estimated"

CAS_CONFLICT_EXIT = 13


class BeadsError(RuntimeError):
    pass


class ClaimConflict(BeadsError):
    """Another worker changed the bead between read and write."""


class Bead(BaseModel):
    id: str
    title: str = ""
    description: str = ""
    design: str = ""
    acceptance_criteria: str = ""
    status: str = ""
    priority: int = 2
    issue_type: str = "task"
    labels: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def recipe(self) -> str | None:
        value = self.metadata.get(META_RECIPE)
        return str(value) if value else None

    @property
    def test_command(self) -> str | None:
        value = self.metadata.get(META_TEST_CMD)
        return str(value) if value else None

    @property
    def complexity_override(self) -> Complexity | None:
        value = self.metadata.get(META_COMPLEXITY)
        return value if value in COMPLEXITY_LEVELS else None

    def task_brief(self) -> str:
        """The human-authored part of the task, as agents should see it."""
        parts = [f"# {self.id}: {self.title}"]
        if self.description:
            parts.append(f"\n## Description\n{self.description}")
        if self.design:
            parts.append(f"\n## Design notes\n{self.design}")
        return "\n".join(parts)


@dataclass
class BeadsClient:
    """Thin, synchronous wrapper over the `bd` CLI."""

    repo: Path
    binary: str = BD_BINARY
    timeout_s: float = 60.0
    env: dict[str, str] = field(default_factory=dict)

    # -- plumbing ---------------------------------------------------------

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        import os

        proc = subprocess.run(
            [self.binary, *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
            env={**os.environ, **self.env},
        )
        if check and proc.returncode != 0:
            raise BeadsError(
                f"bd {' '.join(args)} failed (exit {proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc

    def _json(self, args: list[str]) -> list[dict[str, Any]]:
        proc = self._run([*args, "--json"])
        payload = _first_json_value(proc.stdout)
        if payload is None:
            return []
        if isinstance(payload, dict):
            return [payload]
        return [row for row in payload if isinstance(row, dict)]

    # -- reads ------------------------------------------------------------

    def available(self) -> bool:
        import shutil

        return shutil.which(self.binary) is not None

    def show(self, bead_id: str) -> Bead:
        rows = self._json(["show", bead_id])
        if not rows:
            raise BeadsError(f"bead {bead_id} not found")
        return Bead.model_validate(rows[0])

    def ready(self, *, recipe: str | None = None, limit: int = 50) -> list[Bead]:
        """Open beads with no active blockers, highest priority first."""
        args = ["ready", "--sort", "priority", "--limit", str(limit)]
        if recipe:
            args += ["--metadata-field", f"{META_RECIPE}={recipe}"]
        else:
            args += ["--has-metadata-key", META_RECIPE]
        beads = [Bead.model_validate(row) for row in self._json(args)]
        beads.sort(key=lambda b: (b.priority, b.id))
        return beads

    def list_by_status(self, status: str) -> list[Bead]:
        rows = self._json(["list", "--status", status, "--limit", "0", "--flat"])
        return [Bead.model_validate(row) for row in rows]

    def alloy_beads(self) -> list[Bead]:
        """Every bead Alloy has ever touched or been assigned."""
        rows = self._json(["list", "--all", "--limit", "0", "--flat",
                           "--has-metadata-key", META_RECIPE])
        return [Bead.model_validate(row) for row in rows]

    # -- writes -----------------------------------------------------------

    def ensure_statuses(self) -> None:
        """Register Alloy's execution statuses with Beads (idempotent)."""
        self._run(["config", "set", "status.custom", CUSTOM_STATUSES])

    def claim(self, bead_id: str, *, expect: str = STATUS_READY) -> bool:
        """Move ready -> implementing, but only if nobody else got there first.

        Returns False on a lost race rather than raising, so the scheduler can
        simply try the next bead.
        """
        proc = self._run(
            ["update", bead_id, "-s", STATUS_IMPLEMENTING, "--if-status", expect],
            check=False,
        )
        if proc.returncode == 0:
            return True
        if proc.returncode == CAS_CONFLICT_EXIT:
            return False
        raise BeadsError(f"claim of {bead_id} failed: {proc.stderr.strip()}")

    def set_status(self, bead_id: str, status: str, *, if_status: str | None = None) -> bool:
        args = ["update", bead_id, "-s", status]
        if if_status:
            args += ["--if-status", if_status]
        proc = self._run(args, check=False)
        if proc.returncode == 0:
            return True
        if proc.returncode == CAS_CONFLICT_EXIT:
            return False
        raise BeadsError(f"status update of {bead_id} failed: {proc.stderr.strip()}")

    def set_metadata(self, bead_id: str, values: dict[str, Any]) -> None:
        if not values:
            return
        args = ["update", bead_id]
        for key, value in values.items():
            args += ["--set-metadata", f"{key}={value}"]
        self._run(args)

    def unset_metadata(self, bead_id: str, keys: list[str]) -> None:
        if not keys:
            return
        args = ["update", bead_id]
        for key in keys:
            args += ["--unset-metadata", key]
        self._run(args, check=False)

    def note(self, bead_id: str, text: str) -> None:
        """Append an execution note. Summaries only -- transcripts stay in logs/."""
        self._run(["note", bead_id, text], check=False)

    def close(self, bead_id: str) -> None:
        self._run(["close", bead_id], check=False)


def _first_json_value(stdout: str) -> Any:
    """bd may print advisory banners before the JSON payload."""
    text = stdout.strip()
    if not text:
        return None
    for index, char in enumerate(text):
        if char in "[{":
            decoder = json.JSONDecoder()
            try:
                value, _ = decoder.raw_decode(text[index:])
            except ValueError:
                continue
            return value
    return None
