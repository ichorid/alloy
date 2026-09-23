"""Beads is the durable project graph and the source of truth for executable work.

Alloy reads readiness and writes execution status back; it never keeps a second
copy of the task list. Per-task workflow detail belongs in LangGraph, not here --
what lands on the bead is status plus a handful of pointers (run id, worktree,
branch, stage).
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from alloy.models import COMPLEXITY_LEVELS, Complexity, ProjectSnapshot
from alloy.paths import project_brief_source

log = logging.getLogger(__name__)
_memories_unavailable_logged = False

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
"""Optional operator hint for the verifier (e.g. `pytest -q tests/oauth`). Alloy
never runs it by itself; the verifier decides which checks to run."""
META_COMPLEXITY = "alloy_complexity"
META_COMPLEXITY_ESTIMATED = "alloy_complexity_estimated"
META_DISCOVERED_IN_RUN = "alloy_discovered_in_run"
META_WORKTREE_OWNER = "alloy_worktree_owner"
META_LAND_STATE = "alloy_land_state"
META_LAND_SHA = "alloy_land_sha"
META_LAND_REPAIR = "alloy_land_repair"

# Labels on beads Alloy files itself. `human` is Beads' own convention, so
# `bd human list` surfaces needs-human bugs without any Alloy-specific query.
LABEL_BUG = "alloy-bug"
LABEL_HUMAN = "human"

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
    blocked_by: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def recipe(self) -> str | None:
        value = self.metadata.get(META_RECIPE)
        return str(value) if value else None

    @property
    def check_hint(self) -> str | None:
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

    def ready(
        self, *, recipe: str | None = None, limit: int = 50,
        include_unassigned: bool = False,
    ) -> list[Bead]:
        """Open beads with no active blockers, highest priority first."""
        args = ["ready", "--sort", "priority", "--limit", str(limit)]
        if recipe:
            args += ["--metadata-field", f"{META_RECIPE}={recipe}"]
        elif not include_unassigned:
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

    def blocked(self) -> list[Bead]:
        rows = self._json(["blocked"])
        return [Bead.model_validate(row) for row in rows]

    def children(self, parent_id: str) -> list[Bead]:
        rows = self._json([
            "list", "--parent", parent_id, "--all", "--limit", "0", "--flat",
        ])
        return [Bead.model_validate(row) for row in rows]

    def epic_for(self, bead_id: str, *, max_depth: int = 3) -> str | None:
        """Return the nearest epic ancestor's id, or None when there isn't one."""
        current = bead_id
        for _ in range(max_depth):
            rows = self._json(["show", current])
            if not rows:
                break
            parent_id = _parent_id(rows[0])
            if not parent_id:
                break
            parent_rows = self._json(["show", parent_id])
            if not parent_rows:
                break
            parent = parent_rows[0]
            if parent.get("issue_type") == "epic":
                return str(parent.get("id") or "")
            current = parent_id
        return None

    def epic_root(self, bead_id: str) -> str | None:
        """Return the top-most epic ancestor's id, or None when there isn't one."""
        try:
            current = bead_id
            root_epic: str | None = None
            while True:
                rows = self._json(["show", current])
                if not rows:
                    break
                parent_id = _parent_id(rows[0])
                if not parent_id:
                    break
                parent_rows = self._json(["show", parent_id])
                if not parent_rows:
                    break
                parent = parent_rows[0]
                if parent.get("issue_type") == "epic":
                    root_epic = str(parent.get("id") or "")
                current = parent_id
            return root_epic
        except BeadsError:
            return None

    def open_descendants(self, epic_id: str) -> list[Bead]:
        """All non-closed descendants of an epic, recursing through sub-epics."""
        try:
            return self._open_descendants(epic_id)
        except BeadsError:
            return []

    def _open_descendants(self, epic_id: str) -> list[Bead]:
        result: list[Bead] = []
        for child in self.children(epic_id):
            if child.issue_type == "epic":
                result.extend(self._open_descendants(child.id))
            elif child.status != STATUS_DONE:
                result.append(child)
        return result

    # -- project memory ---------------------------------------------------

    def memories(self) -> dict[str, str]:
        """Read project memories, tolerating bd versions without this command."""
        global _memories_unavailable_logged

        proc = self._run(["memories", "--json"], check=False)
        if proc.returncode != 0:
            if "unknown command" in proc.stderr:
                if not _memories_unavailable_logged:
                    log.warning("bd memories is unavailable; returning no project memories")
                    _memories_unavailable_logged = True
                return {}
            raise BeadsError(
                f"bd memories --json failed (exit {proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        payload = _first_json_value(proc.stdout)
        if payload is None:
            return {}
        return {key: value for key, value in payload.items() if key != "schema_version"}

    def remember(self, key: str, content: str) -> None:
        self._run(["remember", content, "--key", key])

    def forget(self, key: str) -> None:
        self._run(["forget", key])

    # -- project context --------------------------------------------------

    def project_snapshot(self, bead_id: str, *, limit: int = 60) -> ProjectSnapshot:
        """The bead graph around `bead_id`, rendered for the scope and triage roles.

        Every `bd` failure (missing binary, unknown bead, no epic) degrades to
        an empty section: the packet informs a judgement, it never blocks one.
        """
        snapshot = ProjectSnapshot(brief_source=project_brief_source(self.repo) or "")

        rows: list[dict[str, Any]] = []
        for status in (STATUS_READY, STATUS_IMPLEMENTING):
            try:
                rows += self._json(["list", "--status", status, "--limit", "0", "--flat"])
            except Exception:
                log.debug("project_snapshot: bd list --status %s failed", status, exc_info=True)
        snapshot.open_beads = [_render_bead_line(row) for row in rows[:limit]]

        try:
            snapshot.epic = self._epic_for(bead_id)
        except Exception:
            log.debug("project_snapshot: epic lookup for %s failed", bead_id, exc_info=True)

        try:
            bugs = self._json(["list", "--all", "--label", LABEL_BUG, "--limit", "0", "--flat"])
            snapshot.filed_bugs = [_render_bead_line(row) for row in bugs[:limit]]
        except Exception:
            log.debug("project_snapshot: bd list --label %s failed", LABEL_BUG, exc_info=True)

        try:
            payload = self._json(["stats"])
            summary = payload[0].get("summary") if payload else None
            snapshot.stats = dict(summary if isinstance(summary, dict) else (payload[0] if payload else {}))
        except Exception:
            log.debug("project_snapshot: bd stats failed", exc_info=True)
        return snapshot

    def _epic_for(self, bead_id: str, *, max_depth: int = 3) -> str:
        """Walk parent-child edges up from `bead_id` to the nearest epic
        (or the top-most parent when no ancestor is an epic)."""
        current = bead_id
        epic: dict[str, Any] | None = None
        for _ in range(max_depth):
            rows = self._json(["show", current])
            if not rows:
                break
            parent_id = _parent_id(rows[0])
            if not parent_id:
                break
            parent_rows = self._json(["show", parent_id])
            if not parent_rows:
                break
            epic = parent_rows[0]
            if epic.get("issue_type") == "epic":
                break
            current = parent_id
        if epic is None:
            return ""
        text = f"{epic.get('id', '')}: {epic.get('title', '')} [{epic.get('status', '')}]"
        description = str(epic.get("description") or "").strip()
        return f"{text}\n{description}" if description else text

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

    def create_bug(
        self,
        *,
        title: str,
        description: str,
        acceptance: str,
        discovered_from: str,
        priority: int | str,
        labels: list[str],
        metadata: dict[str, Any],
        claim: bool = False,
    ) -> str:
        """File a bug bead discovered while running `discovered_from`.

        The new bead is linked `discovered-from` the parent (which does not
        block it). Callers pass the parent's recipe/test-command metadata plus
        META_DISCOVERED_IN_RUN; `claim=True` moves it straight to implementing
        so a polling scheduler cannot grab it before the parent's child run.
        """
        if str(priority).strip().upper() in ("0", "P0"):
            raise ValueError("bug beads never get priority 0: an agent-filed bead "
                             "must not outrank every human-prioritised bead")
        body = f"{description}\n\nReported by Alloy while running bead {discovered_from}."
        args = [
            "create", title,
            "--type", "bug",
            "--silent",
            "--priority", str(priority),
            "--description", body,
            "--acceptance", acceptance,
            "--deps", f"discovered-from:{discovered_from}",
            "--metadata", json.dumps(metadata),
        ]
        if labels:
            args += ["--labels", ",".join(labels)]
        proc = self._run(args)
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            raise BeadsError(f"bd create --silent returned no id: {proc.stderr.strip()}")
        new_id = lines[-1]
        if claim and not self.claim(new_id):
            raise BeadsError(f"could not claim freshly created bug bead {new_id}")
        return new_id

    def create_task(self, *, title: str, description: str, labels: list[str]) -> str:
        """Create a plain task bead and return its id."""
        args = ["create", title, "--type", "task", "--description", description]
        if labels:
            args += ["--labels", ",".join(labels)]
        args.append("--silent")
        proc = self._run(args)
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            raise BeadsError(f"bd create --silent returned no id: {proc.stderr.strip()}")
        return lines[-1]

    def open_by_label(self, label: str) -> list[Bead]:
        """Open beads carrying `label`."""
        rows = self._json(["list", "--label", label, "--status", "open", "--limit", "0", "--flat"])
        return [Bead.model_validate(row) for row in rows]

    def add_dependency(self, bead_id: str, depends_on_id: str) -> None:
        """Make `bead_id` blocked by `depends_on_id` (bd's default `blocks` edge)."""
        self._run(["dep", "add", bead_id, depends_on_id])


def _parent_id(row: dict[str, Any]) -> str | None:
    """`bd show --json` exposes the parent both as a field and as a
    `parent-child` dependency; accept either."""
    parent = row.get("parent")
    if parent:
        return str(parent)
    for dep in row.get("dependencies") or []:
        if isinstance(dep, dict) and dep.get("dependency_type") == "parent-child":
            return str(dep.get("id") or dep.get("depends_on_id") or "") or None
    return None


def _render_bead_line(row: dict[str, Any]) -> str:
    return (
        f"{row.get('id', '?')} [{row.get('issue_type', 'task')} "
        f"P{row.get('priority', '?')} {row.get('status', '')}] {row.get('title', '')}"
    )


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
