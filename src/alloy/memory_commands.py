"""memory commands domain behavior for the CLI."""

from __future__ import annotations

from typing import Any

from rich.table import Table

from alloy import beads as bd
from alloy.cli_common import _emit, _engine, _fail, _run_async, console, err
from alloy.config import (
    ConfigError,
    MemorySpec,
)
from alloy.engine import Engine
from alloy.memory_schedule import (
    apply_review,
    embed_instruction_files,
    review_plan,
    review_run_id,
)
from alloy.models import (
    ProjectMemory,
    ReviewPlan,
    memory_inventory,
    utcnow,
)


def memory_list_impl(repo, root, json):
    """List every non-meta memory: key, owner, provenance, age in days and
    flags (contradiction recorded, embedded, proposal pending)."""
    engine = _engine(repo, root)
    try:
        memories = engine.beads.memories()
    except bd.BeadsError as exc:
        _fail(str(exc))
        return
    memory = ProjectMemory.from_raw(memories, MemorySpec())
    rows = memory_inventory(memory, utcnow().date())
    if json:
        _emit(rows, True)
        return
    if not rows:
        console.print("no project memories")
        return
    table = Table(show_header=True, header_style="bold")
    # The key is what a reader greps for, so it is never cut or wrapped: it
    # keeps its full width and the other columns fold when the terminal is
    # narrow.
    table.add_column("key", no_wrap=True, min_width=max(len(row["key"]) for row in rows))
    for column in ("owner", "run", "bead", "date", "age", "flags"):
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(
            row["key"],
            row["owner"],
            row["run_id"] or "-",
            row["bead_id"] or "-",
            row["date"] or "-",
            str(row["age_days"]) if row["age_days"] is not None else "-",
            ", ".join(row["flags"]) or "-",
        )
    console.print(table)


def _review_plan(engine: Engine, recipe_name: str, memory: ProjectMemory, run_id: str) -> ReviewPlan:
    """Run the read-only memory review (see memory_schedule.review_plan)."""
    try:
        return _run_async(review_plan(engine, recipe_name, memory, run_id, today=utcnow().date()))
    except ConfigError as exc:
        _fail(str(exc))
        raise  # unreachable: _fail exits


def _apply_review(engine: Engine, plan: ReviewPlan, run_id: str) -> dict[str, Any]:
    """Execute the plan (see memory_schedule.apply_review)."""
    return apply_review(engine, plan, run_id, today=utcnow().date())


def render_memory_review(plan, applied, json) -> None:
    if json:
        payload = plan.model_dump(exclude_none=True)
        if applied is not None:
            payload["applied"] = applied
        _emit(payload, True)
        return
    if not plan.reviewer_ok:
        err.print(f"[yellow]memory_reviewer unavailable:[/yellow] {plan.reviewer_reason}")
    if not plan.items and applied is None:
        console.print("nothing to do")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("key", no_wrap=True)
    for column in ("action", "source", "reason"):
        table.add_column(column, overflow="fold")
    for item in plan.items:
        table.add_row(item.key, item.action, item.source, item.reason)
    if plan.items:
        console.print(table)
    if applied is not None:
        console.print(
            f"applied: forgot {len(applied['forgotten'])}, "
            f"remembered {len(applied['remembered'])}, "
            f"proposed {len(applied['proposals'])}"
            + (
                f" (review bead {applied['review_bead']}{', new' if applied['review_bead_created'] else ', reused'})"
                if applied["review_bead"]
                else ""
            )
        )


def memory_review_impl(repo, root, recipe, apply, json):
    """Plan a project-memory review: deterministic hygiene (expired alloy
    memories, orphan contradiction flags, duplicate bodies) plus the
    memory_reviewer role's keep/update/forget/embed verdicts. Read-only
    unless --apply is given."""
    engine = _engine(repo, root)
    try:
        memories = engine.beads.memories()
    except bd.BeadsError as exc:
        _fail(str(exc))
        return
    try:
        config = engine.load_config(recipe)
    except ConfigError as exc:
        _fail(str(exc))
        return
    memory = ProjectMemory.from_raw(memories, config.memory)
    run_id = review_run_id()
    plan = _review_plan(engine, recipe, memory, run_id)
    applied: dict[str, Any] | None = None
    if apply:
        try:
            applied = _apply_review(engine, plan, run_id)
        except bd.BeadsError as exc:
            _fail(str(exc))
            return
    render_memory_review(plan, applied, json)


def memory_embed_impl(repo, root, recipe):
    """Render the alloy:meta:embed set into the managed block of every
    existing instruction file (memory.instruction_files) and print the files
    that changed. Never runs git."""
    engine = _engine(repo, root)
    try:
        memories = engine.beads.memories()
    except bd.BeadsError as exc:
        _fail(str(exc))
        return
    try:
        config = engine.load_config(recipe)
    except ConfigError as exc:
        _fail(str(exc))
        return
    memory = ProjectMemory.from_raw(memories, config.memory)
    for name in embed_instruction_files(engine.repo, memory, config.memory):
        console.print(name)
