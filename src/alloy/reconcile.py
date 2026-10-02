"""Bring Beads status, Alloy's bead metadata and the run ledger back in step.

Three places describe the same bead -- its bd status, the `alloy_*` metadata
on it, and the `runs` table -- and a manual `bd close`, a cancel or a crash can
leave them telling different stories. A drift that nothing reconciles looks
exactly like a hung scheduler (journal-operating-tentura 1, 2, 13):
`reconcile` names every mismatch and, with `apply`, fixes the ones that have a
safe, mechanical fix. Anything that needs judgement is only reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from alloy import beads as bd
from alloy.engine import Engine
from alloy.models import Outcome
from alloy.procs import pid_alive, read_pid
from alloy.store import RUN_CANCELLED, RUN_DONE, RUN_FAILED, RUN_RUNNING, RUN_WAITING_HUMAN

ACTIVE = (RUN_RUNNING, RUN_WAITING_HUMAN)
LAND_KEYS = [bd.META_LAND_STATE, bd.META_LAND_REPAIR, bd.META_LAND_ATTEMPTS]


@dataclass
class Finding:
    bead: str
    problem: str
    fix: str
    action: Callable[[], None] | None = field(default=None, repr=False)
    applied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "bead": self.bead,
            "problem": self.problem,
            "fix": self.fix,
            "fixable": self.action is not None,
            "applied": self.applied,
        }


def reconcile(engine: Engine, bead_ids: list[str] | None = None, *, apply: bool = False) -> list[Finding]:
    """Every mismatch for `bead_ids` (default: `_touched_beads`'s bounded set).

    Reads happen inside one `snapshot()` -- a single bulk `bd list` instead of
    one `bd show` subprocess per bead (the actual cost of a full pass: ~394
    beads at ~0.25s each is minutes, the bulk fetch is one call). Writes, in
    `--apply`, happen afterward and are never snapshotted.
    """
    scheduler_pid = read_pid(engine.paths.scheduler_pid)
    findings: list[Finding] = []
    with engine.beads.snapshot():
        ids = bead_ids or _touched_beads(engine)
        resolved = engine.beads.show_many(ids)
        for bead_id in ids:
            bead = resolved.get(bead_id)
            if bead is None:
                findings.append(Finding(bead_id, "bd cannot show it", "none; check the id"))
                continue
            findings += _check_bead(engine, bead, scheduler_pid)
    if apply:
        for finding in findings:
            if finding.action is not None:
                finding.action()
                finding.applied = True
    return findings


def _touched_beads(engine: Engine) -> list[str]:
    """Every bead worth checking: active runs (where bd and the run ledger
    could have drifted -- the outbox makes Alloy's own writes land together,
    but bd still accepts writes Alloy never asked for, like a manual `bd
    close`), every bead that has ever carried a recipe (one bulk `bd list`
    call, needed for the legacy-land-metadata and manual-bead-with-recipe
    checks below -- not one `bd show` per bead), and beads currently in a
    status only Alloy's own write paths should produce. Not every bead Alloy
    has ever run a workflow for -- that was `all_runs(limit=10_000)`, scanning
    every run of every bead ever, the actual source of a 10+ minute pass over
    this project's history.
    """
    ids = {run["bead_id"] for run in engine.store.active_runs(engine.repo)}
    ids |= {bead.id for bead in engine.beads.alloy_beads()}
    for status in (bd.STATUS_REVIEW_READY, bd.STATUS_IMPLEMENTING, bd.STATUS_WAITING_HUMAN):
        ids |= {bead.id for bead in engine.beads.list_by_status(status)}
    # A closed bead whose `alloy_recipe` was already cleaned up (e.g. by the
    # manual-bead-with-recipe fix below) drops out of `alloy_beads()` -- but
    # could still carry stale land metadata from before that cleanup. Each of
    # these is its own bounded, self-shrinking `bd list` call, not a reason to
    # fall back to scanning everything.
    for key in LAND_KEYS:
        ids |= {bead.id for bead in engine.beads.with_metadata_key(key)}
    return sorted(ids)


def _check_bead(engine: Engine, bead: bd.Bead, scheduler_pid: int | None) -> list[Finding]:
    findings: list[Finding] = []
    record = engine.store.latest_run_for_bead(bead.id)
    active = record is not None and record["status"] in ACTIVE
    closed = bead.status == bd.STATUS_DONE

    if active and closed:
        findings.append(
            Finding(
                bead.id,
                f"closed in bd, but run {record['run_id']} is still {record['status']}",
                "book the run cancelled; the bead stays closed",
                lambda: _cancel_run(engine, record),
            )
        )
    elif active and record["status"] == RUN_RUNNING and not pid_alive(record.get("pid")):
        findings.append(
            Finding(
                bead.id,
                f"run {record['run_id']} is marked running but its process (pid {record.get('pid')}) is gone",
                f"book it cancelled and reopen the bead (or `alloy run {bead.id}` to adopt it instead)",
                lambda: _cancel_run(engine, record),
            )
        )
    elif active and record["status"] == RUN_RUNNING and scheduler_pid and record.get("pid") == scheduler_pid:
        pass  # the live scheduler is running it: nothing is out of step

    if not active and bead.status == bd.STATUS_IMPLEMENTING:
        target = _status_after(record)
        last = f"its last run {record['run_id']} is {record['status']}" if record else "no run is recorded"
        if target is None:
            findings.append(
                Finding(
                    bead.id,
                    f"bd says implementing, but {last}, and the commit was never confirmed "
                    "-- the work may not actually be saved",
                    "none; verify by hand whether the commit landed, then close or reopen it",
                )
            )
        else:
            findings.append(
                Finding(
                    bead.id,
                    f"bd says implementing, but {last}",
                    f"set the bead to {target}",
                    lambda: engine.beads.set_status(bead.id, target),
                )
            )
    if not active and bead.status == bd.STATUS_WAITING_HUMAN and bead.metadata.get(bd.META_LAND_STATE) != "parked":
        findings.append(
            Finding(
                bead.id,
                "bd says waiting-human, but no run is parked at a human gate",
                f"set the bead to {bd.STATUS_READY}",
                lambda: engine.beads.set_status(bead.id, bd.STATUS_READY),
            )
        )

    stale_land = [key for key in LAND_KEYS if key in bead.metadata]
    if closed and bead.metadata.get(bd.META_LAND_STATE) not in (None, "landed") and stale_land:
        findings.append(
            Finding(
                bead.id,
                f"closed, but still carries land metadata ({bead.metadata.get(bd.META_LAND_STATE)}) "
                "that can re-trigger a land retry",
                f"unset {', '.join(stale_land)}",
                lambda: engine.beads.unset_metadata(bead.id, stale_land),
            )
        )
    if bead.status == bd.STATUS_REVIEW_READY:
        findings += _check_review_ready(engine, bead)

    if bead.manual and bead.recipe:
        findings.append(
            Finding(
                bead.id,
                f"human-operated (manual) but carries {bd.META_RECIPE}={bead.recipe}",
                f"unset {bd.META_RECIPE} (the scheduler already skips it)",
                lambda: engine.beads.unset_metadata(bead.id, [bd.META_RECIPE]),
            )
        )
    return findings


def _check_review_ready(engine: Engine, bead: bd.Bead) -> list[Finding]:
    """A review-ready bead holds top-level dispatch for its unit. Alloy has no
    separate land step any more (a run finishes straight to `done`), so this
    only happens from old data (set before that removal) or a manual `bd
    update -s review-ready`; either way the bead itself decides it, not a
    land command that no longer exists."""
    repair = bead.metadata.get(bd.META_LAND_REPAIR)
    if bead.metadata.get(bd.META_LAND_STATE) == "repairing" and repair:
        try:
            status = engine.beads.show(str(repair)).status
        except bd.BeadsError:
            return [
                Finding(
                    bead.id,
                    f"review-ready, waiting on land-repair bug {repair}, which bd cannot find",
                    f"unset {bd.META_LAND_STATE}/{bd.META_LAND_REPAIR}; then close {bead.id} or reopen it",
                    lambda: engine.beads.unset_metadata(bead.id, [bd.META_LAND_STATE, bd.META_LAND_REPAIR]),
                )
            ]
        if status == bd.STATUS_DONE:
            return [
                Finding(
                    bead.id,
                    f"review-ready; its old land-repair bug {repair} is closed",
                    f"close {bead.id} now (or reopen it if it still needs work)",
                )
            ]
        return [
            Finding(
                bead.id,
                f"review-ready and holding dispatch until land-repair bug {repair} ({status}) is fixed",
                f"fix {repair}, or close {bead.id} by hand if it needs no further work",
            )
        ]
    return [
        Finding(
            bead.id,
            "review-ready: it holds top-level dispatch until a human closes or reopens it",
            f"close {bead.id} now (or reopen it if it still needs work)",
        )
    ]


def _status_after(record: dict[str, Any] | None) -> str | None:
    """The bd status a drifted `implementing` bead should heal to, or `None`
    when that can't be decided automatically and a human should look."""
    if record is None or record["status"] == RUN_CANCELLED:
        return bd.STATUS_READY
    if record["status"] == RUN_FAILED:
        return bd.STATUS_FAILED
    if record["status"] == RUN_DONE:
        # engine.py sets STATUS_DONE directly on a finished run; there is no
        # separate land step any more, so a drifted bd status heals the same
        # way -- never to STATUS_REVIEW_READY, a state nothing can clear any
        # more since `alloy land` was removed. But only once `committed_sha`
        # confirms the commit itself actually happened: a crash or a failed
        # `commit_wip` between the commit and recording it would otherwise
        # let this auto-heal close a bead whose work was never saved.
        return bd.STATUS_DONE if record.get("committed_sha") else None
    return bd.STATUS_READY


def _cancel_run(engine: Engine, record: dict[str, Any]) -> None:
    engine.store.finish_run(
        record["run_id"],
        status=RUN_CANCELLED,
        outcome=Outcome.CANCELLED.value,
        reason="reconciled: no live process owned it",
    )
    if engine._bead_status(record["bead_id"]) != bd.STATUS_DONE:
        engine.beads.set_status(record["bead_id"], bd.STATUS_READY)
    engine.beads.note(record["bead_id"], f"alloy: reconcile booked run {record['run_id']} cancelled")


# -- assign-recipe (journal-operating-tentura 4, 5) -------------------------


def recipe_candidates(engine: Engine, recipe: str, *, from_recipe: str | None = None) -> list[bd.Bead]:
    """Open beads pinned to another recipe that `alloy assign-recipe` would
    retarget. Epics and human-operated beads are never swept up: a bulk
    update that armed a merge-gate checklist for autonomous dispatch had it
    "implemented" by an agent."""
    found: list[bd.Bead] = []
    for bead in engine.beads.list_by_status(bd.STATUS_READY):
        if bead.issue_type == "epic" or bead.manual or not bead.recipe or bead.recipe == recipe:
            continue
        if from_recipe is not None and bead.recipe != from_recipe:
            continue
        found.append(bead)
    return sorted(found, key=lambda b: (b.priority, b.id))
