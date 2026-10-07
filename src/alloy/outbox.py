"""Deliver pending `bd_outbox` rows.

A local SQLite commit (a run's status transition plus its enqueued bd-facing
side effects, written together) is the one authoritative write. Pushing that
commit out to bd -- an external process, no shared transaction -- is a
replayable side effect, never a second authoritative write that can half-land.
Delivery is safe to call as often as useful: an already-delivered row is
skipped (`pending_bd_updates` only returns undelivered ones), and a `status`
row's compare-and-set is itself idempotent to repeat.

Ownership fencing. A status-only CAS (`--if-status implementing`) cannot tell
*which* run put bd in that status: a stale terminal write from an old run A,
stuck undelivered through an outage, would still match after a human reopened
the bead and a newer run B claimed it -- and close or fail B's work. So every
row a run enqueues (`bd_outbox.run_id` set) that *changes* bd's state --
`status`, `metadata`, `close` -- is delivered only while bd's `alloy_run_id`
still names that run. `BeadsClient.claim` stamps `alloy_run_id` atomically
with its status CAS, so the stamp is exactly "the run whose claim landed
last"; a run that no longer owns the bead has nothing left to say to it, and
its row is consumed as a conflict, like a lost CAS. Notes are history about
their own run and are delivered regardless. bd has no metadata CAS, so the
check is a read just before the (still status-CAS-guarded) write: the
remaining window needs two foreign writes (a reopen *and* a new claim)
landing between that read and this write, versus "any time during an
outage" before.

A `status` payload may give `if_status_in` (a list) instead of `if_status`:
the status read for the ownership check is then CAS-guarded on whichever of
those it is -- for a transition that is right from several states (a cancel
reverting `implementing` *or* `waiting-human` to ready) without ever
degrading to an unconditional write.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from alloy.beads import META_RUN_ID, BeadsClient, BeadsError
from alloy.store import Store

log = logging.getLogger("alloy.outbox")


@dataclass
class DeliveryResult:
    outbox_id: int
    bead_id: str
    kind: str
    ok: bool
    conflict: bool = False
    fenced: bool = False
    error: str | None = None


_FENCED_KINDS = frozenset({"status", "metadata", "label", "close"})


@dataclass
class _BeadView:
    """What delivery knows of one bead's bd state during one sweep: read once
    (lazily, only if a fenced row needs it) and kept current with this
    sweep's own writes, instead of a `bd show` per row."""

    status: str
    owner: str | None


def deliver_pending(store: Store, beads: BeadsClient, *, bead_id: str | None = None) -> list[DeliveryResult]:
    """Deliver every pending row, in id order. Per bead, a failed row (a
    transient bd error, not a CAS conflict) stops delivery for *that bead*
    for this call -- a later row might be a status transition that assumed
    the failed one already landed (`implementing -> closed` assuming
    `open -> implementing` succeeded); delivering it anyway would either
    silently reorder bd's history or manufacture a CAS conflict that looks
    like a human intervened when the real cause was Alloy's own failed
    delivery. Other beads are unaffected. `bead_id=None` sweeps every bead
    with something pending, which is the bounded set
    `Store.pending_bd_bead_ids` names, not Alloy's whole history."""
    results: list[DeliveryResult] = []
    stalled: set[str] = set()
    views: dict[str, _BeadView] = {}
    for row in store.pending_bd_updates(bead_id):
        owner = row["bead_id"]
        if owner in stalled:
            continue
        result = _deliver_one(store, beads, row, views)
        results.append(result)
        if not result.ok:
            stalled.add(owner)
            views.pop(owner, None)
    return results


def _view(beads: BeadsClient, bead_id: str, views: dict[str, _BeadView]) -> _BeadView:
    view = views.get(bead_id)
    if view is None:
        bead = beads.show(bead_id)  # BeadsError: a transient failure like any other
        owner = bead.metadata.get(META_RUN_ID)
        view = views[bead_id] = _BeadView(status=bead.status, owner=str(owner) if owner else None)
    return view


def _deliver_one(
    store: Store, beads: BeadsClient, row: dict[str, Any], views: dict[str, _BeadView] | None = None
) -> DeliveryResult:
    views = {} if views is None else views
    payload = json.loads(row["payload_json"])
    bead_id = row["bead_id"]
    kind = row["kind"]
    run_id = row.get("run_id")
    try:
        view: _BeadView | None = None
        if run_id and kind in _FENCED_KINDS:
            view = _view(beads, bead_id, views)
            # No stamp at all (a bead no Alloy claim ever touched) has no
            # newer owner to protect; any *other* stamp does.
            if view.owner is not None and view.owner != run_id:
                store.mark_bd_delivered(row["id"])
                log.info(
                    "%s: outbox #%s (%s) from run %s fenced off: bd now belongs to run %s; not delivered",
                    bead_id,
                    row["id"],
                    kind,
                    run_id,
                    view.owner,
                )
                return DeliveryResult(row["id"], bead_id, kind, ok=True, conflict=True, fenced=True)
        if kind == "status":
            return _deliver_status(store, beads, row, payload, view, views)
        if kind == "metadata":
            beads.set_metadata(bead_id, payload["values"])
            if bead_id in views and META_RUN_ID in payload["values"]:
                views[bead_id].owner = str(payload["values"][META_RUN_ID])
        elif kind == "note":
            # check=True: note()/close() default to best-effort (never raise)
            # for their many direct callers, but a silently swallowed failure
            # here would mark an undelivered row delivered forever.
            beads.note(bead_id, payload["text"], check=True)
        elif kind == "label":
            beads.add_label(bead_id, payload["label"], check=True)
        elif kind == "close":
            beads.close(bead_id, check=True)
            views.pop(bead_id, None)
        else:
            raise ValueError(f"unknown bd_outbox kind: {kind!r}")
        store.mark_bd_delivered(row["id"])
        return DeliveryResult(row["id"], bead_id, kind, ok=True)
    except BeadsError as exc:
        store.mark_bd_delivered(row["id"], error=str(exc))
        log.warning("%s: outbox #%s (%s) delivery failed, will retry: %s", bead_id, row["id"], kind, exc)
        return DeliveryResult(row["id"], bead_id, kind, ok=False, error=str(exc))


def _deliver_status(
    store: Store,
    beads: BeadsClient,
    row: dict[str, Any],
    payload: dict[str, Any],
    view: _BeadView | None,
    views: dict[str, _BeadView],
) -> DeliveryResult:
    bead_id, kind = row["bead_id"], row["kind"]
    if_status = payload.get("if_status")
    allowed = payload.get("if_status_in")
    if allowed is not None:
        current = view.status if view is not None else _view(beads, bead_id, views).status
        if current not in allowed:
            store.mark_bd_delivered(row["id"])
            log.info(
                "%s: outbox #%s status -> %s skipped: bd is %r, not one of %s",
                bead_id,
                row["id"],
                payload["status"],
                current,
                allowed,
            )
            return DeliveryResult(row["id"], bead_id, kind, ok=True, conflict=True)
        if_status = current
    ok = beads.set_status(bead_id, payload["status"], if_status=if_status)
    store.mark_bd_delivered(row["id"])
    if ok:
        if bead_id in views:
            views[bead_id].status = payload["status"]
    else:
        views.pop(bead_id, None)  # bd moved under us: re-read before trusting it again
        # Something else -- most likely a human -- already moved bd
        # away from the status this transition assumed. The row is
        # still consumed (there is nothing to retry towards: the
        # precondition it was written under no longer holds), but the
        # conflict is worth a human noticing, not silently dropping.
        log.info(
            "%s: outbox #%s status -> %s (if %s) lost the race in bd; not retried",
            bead_id,
            row["id"],
            payload["status"],
            if_status,
        )
    return DeliveryResult(row["id"], bead_id, kind, ok=True, conflict=not ok)
