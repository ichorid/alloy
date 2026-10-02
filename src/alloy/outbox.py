"""Deliver pending `bd_outbox` rows.

A local SQLite commit (a run's status transition plus its enqueued bd-facing
side effects, written together) is the one authoritative write. Pushing that
commit out to bd -- an external process, no shared transaction -- is a
replayable side effect, never a second authoritative write that can half-land.
Delivery is safe to call as often as useful: an already-delivered row is
skipped (`pending_bd_updates` only returns undelivered ones), and a `status`
row's compare-and-set is itself idempotent to repeat.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from alloy.beads import BeadsClient, BeadsError
from alloy.store import Store

log = logging.getLogger("alloy.outbox")


@dataclass
class DeliveryResult:
    outbox_id: int
    bead_id: str
    kind: str
    ok: bool
    conflict: bool = False
    error: str | None = None


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
    for row in store.pending_bd_updates(bead_id):
        owner = row["bead_id"]
        if owner in stalled:
            continue
        result = _deliver_one(store, beads, row)
        results.append(result)
        if not result.ok:
            stalled.add(owner)
    return results


def _deliver_one(store: Store, beads: BeadsClient, row: dict[str, Any]) -> DeliveryResult:
    payload = json.loads(row["payload_json"])
    bead_id = row["bead_id"]
    kind = row["kind"]
    try:
        if kind == "status":
            ok = beads.set_status(bead_id, payload["status"], if_status=payload.get("if_status"))
            store.mark_bd_delivered(row["id"])
            if not ok:
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
                    payload.get("if_status"),
                )
            return DeliveryResult(row["id"], bead_id, kind, ok=True, conflict=not ok)
        if kind == "metadata":
            beads.set_metadata(bead_id, payload["values"])
        elif kind == "note":
            # check=True: note()/close() default to best-effort (never raise)
            # for their many direct callers, but a silently swallowed failure
            # here would mark an undelivered row delivered forever.
            beads.note(bead_id, payload["text"], check=True)
        elif kind == "close":
            beads.close(bead_id, check=True)
        else:
            raise ValueError(f"unknown bd_outbox kind: {kind!r}")
        store.mark_bd_delivered(row["id"])
        return DeliveryResult(row["id"], bead_id, kind, ok=True)
    except BeadsError as exc:
        store.mark_bd_delivered(row["id"], error=str(exc))
        log.warning("%s: outbox #%s (%s) delivery failed, will retry: %s", bead_id, row["id"], kind, exc)
        return DeliveryResult(row["id"], bead_id, kind, ok=False, error=str(exc))
