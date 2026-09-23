"""Scheduled memory maintenance (alloy-4ef.19): when a review is due, how to
plan and apply it, and when the embed step must be skipped.

``review_due`` and ``dirty_instruction_files`` decide; ``review_plan``,
``apply_review`` and ``embed_instruction_files`` are the shared execution
steps behind both ``alloy memory review/embed`` and ``Scheduler.tick``.
The CLI wraps them; the scheduler awaits them. Nothing here shells out to
``alloy`` itself, and the only git call is a read-only ``git status``.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alloy import beads as bd
from alloy.config import ConfigError, MemorySpec
from alloy.memory_embed import last_review_date, render_embed_block, splice_managed_block
from alloy.models import (
    EMBED_STALE_KEY, MEMORY_REVIEW_LABEL, ProjectMemory, ReviewApply, ReviewPlan,
    plan_review_apply, review_bead_text,
)

if TYPE_CHECKING:
    from alloy.engine import Engine

log = logging.getLogger("alloy.memory_schedule")

MEMORY_REVIEW_RECIPE = "tdd-loop"
MEMORY_REVIEW_BEAD = "memory-review"

_FALSE_FLAGS = frozenset({"", "0", "false", "no", "off"})


def review_run_id() -> str:
    return f"memory-review-{uuid.uuid4().hex[:12]}"


# -- due / skip decisions (pure) -------------------------------------------


def embed_stale(memory: ProjectMemory) -> bool:
    """True when ``alloy:meta:embed-stale`` is present with a truthy body."""

    if EMBED_STALE_KEY not in memory.entries:
        return False
    return memory.body_of(EMBED_STALE_KEY).strip().lower() not in _FALSE_FLAGS


def review_due(
    memory: ProjectMemory,
    spec: MemorySpec,
    *,
    today: date,
    finished_runs: int,
    last_ran_day: date | None,
) -> str | None:
    """Why a review is due today, or None.

    Due when the last review is ``review_every_days`` or more days old, when
    more than ``review_every_runs`` runs finished since it, or when the
    embed-stale flag is set -- but never twice on one calendar day. A project
    with no recorded review is not due by age alone: the run count and the
    stale flag decide, so a fresh repo is not reviewed on its first tick.
    """

    if last_ran_day == today:
        return None
    reviewed = last_review_date(memory)
    if reviewed is not None:
        age = (today - reviewed).days
        if age >= spec.review_every_days:
            return f"last review {age} days ago (every {spec.review_every_days})"
    if finished_runs > spec.review_every_runs:
        return f"{finished_runs} runs finished since last review (every {spec.review_every_runs})"
    if embed_stale(memory):
        return f"{EMBED_STALE_KEY} is set"
    return None


def dirty_instruction_files(repo: Path, names: list[str]) -> list[str]:
    """Existing instruction files with uncommitted changes, per a read-only
    ``git status --porcelain``. Empty when git is unavailable or clean."""

    existing = [name for name in names if (repo / name).is_file()]
    if not existing:
        return []
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all", "--", *existing],
            cwd=str(repo), capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        log.warning("git status failed in %s: %s", repo, exc)
        return []
    if proc.returncode != 0:
        log.warning("git status failed in %s: %s", repo, proc.stderr.strip())
        return []
    dirty: list[str] = []
    for line in proc.stdout.splitlines():
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path in existing and path not in dirty:
            dirty.append(path)
    return sorted(dirty)


# -- execution steps ---------------------------------------------------------


async def review_plan(engine: Engine, recipe_name: str, memory: ProjectMemory,
                      run_id: str, *, today: date) -> ReviewPlan:
    """Run the read-only memory review under a throwaway RunContext bound to
    the repository itself (no worktree, no bead, no ledger run row)."""
    from alloy.recipes.tdd_loop import review_memory
    from alloy.runners import RunnerRegistry
    from alloy.runtime import RunContext
    from alloy.worktree import Worktree, WorktreeManager

    config = engine.load_config(recipe_name)
    if "memory_reviewer" not in config.roles:
        raise ConfigError(f"recipe {recipe_name} has no memory_reviewer role")
    log_dir = engine.paths.logs / "memory-review" / run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    ctx = RunContext(
        bead=bd.Bead(id=MEMORY_REVIEW_BEAD, title="alloy memory review"),
        recipe=config,
        run_id=run_id,
        worktree=Worktree(bead_id=MEMORY_REVIEW_BEAD, path=engine.repo, branch="", base_commit=""),
        worktrees=WorktreeManager(repo=engine.repo, root=engine.paths.worktrees),
        registry=RunnerRegistry(config.runners, log_dir=log_dir),
        store=engine.store,
        checkpointer=None,
        log_dir=log_dir,
        beads=engine.beads,
    )
    return await review_memory(ctx, memory, today=today)


def apply_review(engine: Engine, plan: ReviewPlan, run_id: str, *, today: date) -> dict[str, Any]:
    """Execute the plan: alloy-owned forgets/updates, proposal memories for
    human-owned keys plus one open alloy-memory-review task bead listing
    them (reused when already open), then the meta keys."""
    apply: ReviewApply = plan_review_apply(
        plan, run_id=run_id, bead_id=MEMORY_REVIEW_BEAD, today=today,
    )
    for key in apply.forgets:
        engine.beads.forget(key)
    for key, content in apply.remembers:
        engine.beads.remember(key, content)
    review_bead: str | None = None
    created = False
    if apply.proposals:
        open_beads = engine.beads.open_by_label(MEMORY_REVIEW_LABEL)
        if open_beads:
            review_bead = open_beads[0].id
        else:
            title, description = review_bead_text(apply.proposals)
            review_bead = engine.beads.create_task(
                title=title, description=description, labels=[MEMORY_REVIEW_LABEL],
            )
            created = True
    return {
        "forgotten": apply.forgets,
        "remembered": [key for key, _ in apply.remembers],
        "proposals": apply.proposals,
        "embed": apply.embed_keys,
        "review_bead": review_bead,
        "review_bead_created": created,
    }


def embed_instruction_files(repo: Path, memory: ProjectMemory, spec: MemorySpec) -> list[str]:
    """Splice the managed block into every existing instruction file; return
    the names that changed. Never runs git."""
    managed = render_embed_block(memory, spec)
    changed: list[str] = []
    for name in spec.instruction_files:
        path = repo / name
        if not path.is_file():
            continue
        updated, differs = splice_managed_block(path.read_text(encoding="utf-8"), managed)
        if differs:
            path.write_text(updated, encoding="utf-8")
            changed.append(name)
    return changed


def review_bead_for_note(engine: Engine, applied: dict[str, Any]) -> str:
    """The open alloy-memory-review bead to carry an operator-visible note,
    creating one when the applied review produced none."""
    if applied.get("review_bead"):
        return str(applied["review_bead"])
    open_beads = engine.beads.open_by_label(MEMORY_REVIEW_LABEL)
    if open_beads:
        return open_beads[0].id
    return engine.beads.create_task(
        title="Memory review follow-up",
        description="Alloy's scheduled memory review needs operator attention; see the notes.",
        labels=[MEMORY_REVIEW_LABEL],
    )
