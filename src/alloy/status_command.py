"""status command domain behavior for the CLI."""

from __future__ import annotations

from rich.table import Table

from alloy import beads as bd
from alloy.cli_common import _emit, _engine, _fail, console
from alloy.scheduler import (
    read_pid,
)
from alloy.status_display import (
    _agent_label,
    _bead_row,
    _coloured,
    _status_sort_key,
    _truncate,
)


def status_impl(bead_id, repo, root, json, limit):
    """Show every bead Alloy tracks -- queued, running, or finished -- with its
    place in the schedule. Designed to be read by humans and by agents."""
    engine = _engine(repo, root)
    scheduler_pid = read_pid(engine.paths.scheduler_pid)

    if bead_id:
        try:
            beads_list = [engine.beads.show(bead_id)]
        except bd.BeadsError as exc:
            _fail(str(exc))
            return
    else:
        beads_list = engine.beads.alloy_beads()

    ready_ids = {b.id for b in engine.beads.ready(limit=max(limit, 1000))}
    queue_order = sorted((b for b in beads_list if b.id in ready_ids), key=lambda b: (b.priority, b.id))
    queue_position = {b.id: index + 1 for index, b in enumerate(queue_order)}

    rows = [_bead_row(engine, b, ready_ids) for b in beads_list]
    for row in rows:
        row["queue_position"] = queue_position.get(row["bead"])
    rows.sort(key=_status_sort_key)
    if not bead_id:
        rows = rows[:limit]

    payload = {
        "root": str(engine.paths.root),
        "repo": str(engine.repo),
        "scheduler": {"running": scheduler_pid is not None, "pid": scheduler_pid},
        "beads": rows,
    }
    if json:
        _emit(payload, True)
        return

    console.print(
        f"scheduler: [{'green' if scheduler_pid else 'yellow'}]"
        f"{'running (pid ' + str(scheduler_pid) + ')' if scheduler_pid else 'stopped'}[/]"
    )
    if not rows:
        console.print("alloy is not tracking any beads yet")
        return
    table = Table(show_header=True, header_style="bold", expand=True)
    # One line per bead, whatever the terminal width: the title gives way
    # first, identifiers and numbers keep their minimum widths.
    for column, min_width in (
        ("parent", 10),
        ("bead", 12),
        ("title", 12),
        ("pri", 3),
        ("queue", 5),
        ("status", 9),
        ("stage", 9),
        ("agent", 14),
        ("iter", 4),
        ("tests", 18),
        ("elapsed", 7),
    ):
        table.add_column(
            column,
            no_wrap=True,
            overflow="ellipsis",
            min_width=min_width,
            ratio=4 if column == "title" else (2 if column == "agent" else None),
        )
    for row in rows:
        queue = str(row["queue_position"]) if row["queue_position"] else "-"
        max_iter = row["max_iterations"] if row["max_iterations"] is not None else "-"
        table.add_row(
            row.get("parent_bead_id") or "-",
            row["bead"],
            _truncate(row["title"], 32),
            str(row["priority"]),
            queue,
            _coloured(row["status"] or row["bead_status"]),
            row["stage"] or "-",
            _agent_label(row),
            f"{row['iteration']}/{max_iter}",
            row["tests"] or "-",
            row["elapsed"],
        )
    console.print(table)
