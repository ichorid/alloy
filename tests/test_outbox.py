"""`bd_outbox`: enqueue, then deliver.

A local SQLite commit is the one authoritative write; delivering it to bd is a
replayable side effect. These tests talk to a throwaway `Store` and a fake,
in-memory stand-in for `BeadsClient` -- `alloy.outbox` only calls four of its
methods (`set_status`, `set_metadata`, `note`, `close`), so there is nothing
to gain from a real `bd` subprocess here (that belongs to test_beads.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alloy.beads import BeadsError
from alloy.outbox import deliver_pending
from alloy.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "alloy.db")


class FakeBeadsClient:
    """Records every call; `set_status` honors `if_status` like the real CAS
    claim does. `fail_next` makes the next call of that method raise
    `BeadsError`, once, to exercise the transient-failure retry path."""

    def __init__(self) -> None:
        self.statuses: dict[str, str] = {}
        self.metadata: dict[str, dict] = {}
        self.notes: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.calls: list[tuple[str, tuple]] = []
        self._fail_next: set[str] = set()

    def fail_next(self, method: str) -> None:
        self._fail_next.add(method)

    def _maybe_fail(self, method: str) -> None:
        if method in self._fail_next:
            self._fail_next.discard(method)
            raise BeadsError(f"simulated {method} failure")

    def set_status(self, bead_id: str, status: str, *, if_status: str | None = None) -> bool:
        self.calls.append(("set_status", (bead_id, status, if_status)))
        self._maybe_fail("set_status")
        current = self.statuses.get(bead_id)
        if if_status is not None and current != if_status:
            return False
        self.statuses[bead_id] = status
        return True

    def set_metadata(self, bead_id: str, values: dict) -> None:
        self.calls.append(("set_metadata", (bead_id, values)))
        self._maybe_fail("set_metadata")
        self.metadata.setdefault(bead_id, {}).update(values)

    def note(self, bead_id: str, text: str) -> None:
        self.calls.append(("note", (bead_id, text)))
        self._maybe_fail("note")
        self.notes.append((bead_id, text))

    def close(self, bead_id: str) -> None:
        self.calls.append(("close", (bead_id,)))
        self._maybe_fail("close")
        self.closed.append(bead_id)


@pytest.fixture
def beads() -> FakeBeadsClient:
    return FakeBeadsClient()


# -- happy path, one per kind -------------------------------------------------


def test_delivers_a_status_update(store: Store, beads: FakeBeadsClient):
    beads.statuses["b-1"] = "implementing"
    store.enqueue_bd_status("b-1", "closed", if_status="implementing")

    results = deliver_pending(store, beads)

    assert len(results) == 1 and results[0].ok and not results[0].conflict
    assert beads.statuses["b-1"] == "closed"
    assert store.pending_bd_updates() == []


def test_delivers_metadata(store: Store, beads: FakeBeadsClient):
    store.enqueue_bd_metadata("b-1", {"alloy_stage": "finished"})

    deliver_pending(store, beads)

    assert beads.metadata["b-1"] == {"alloy_stage": "finished"}
    assert store.pending_bd_updates() == []


def test_delivers_a_note(store: Store, beads: FakeBeadsClient):
    store.enqueue_bd_note("b-1", "alloy: done")

    deliver_pending(store, beads)

    assert beads.notes == [("b-1", "alloy: done")]
    assert store.pending_bd_updates() == []


def test_delivers_a_close(store: Store, beads: FakeBeadsClient):
    store.enqueue_bd_close("b-1")

    deliver_pending(store, beads)

    assert beads.closed == ["b-1"]
    assert store.pending_bd_updates() == []


# -- ordering, scoping --------------------------------------------------------


def test_delivers_rows_for_one_bead_in_id_order(store: Store, beads: FakeBeadsClient):
    beads.statuses["b-1"] = "open"
    store.enqueue_bd_status("b-1", "implementing", if_status="open")
    store.enqueue_bd_status("b-1", "closed", if_status="implementing")

    deliver_pending(store, beads)

    assert [c[1][1] for c in beads.calls if c[0] == "set_status"] == ["implementing", "closed"]
    assert beads.statuses["b-1"] == "closed"


def test_bead_id_scopes_delivery_to_one_bead(store: Store, beads: FakeBeadsClient):
    store.enqueue_bd_note("b-1", "for b-1")
    store.enqueue_bd_note("b-2", "for b-2")

    deliver_pending(store, beads, bead_id="b-1")

    assert beads.notes == [("b-1", "for b-1")]
    pending_ids = {row["bead_id"] for row in store.pending_bd_updates()}
    assert pending_ids == {"b-2"}


def test_pending_bd_bead_ids_lists_only_beads_with_something_undelivered(store: Store, beads: FakeBeadsClient):
    store.enqueue_bd_note("b-1", "x")
    store.enqueue_bd_note("b-2", "y")
    deliver_pending(store, beads, bead_id="b-1")

    assert store.pending_bd_bead_ids() == ["b-2"]


# -- CAS conflict: consumed, not retried --------------------------------------


def test_a_cas_conflict_is_consumed_not_retried(store: Store, beads: FakeBeadsClient):
    beads.statuses["b-1"] = "closed"  # a human already moved it away from "implementing"
    store.enqueue_bd_status("b-1", "failed", if_status="implementing")

    results = deliver_pending(store, beads)

    assert results[0].ok and results[0].conflict
    assert beads.statuses["b-1"] == "closed"  # untouched by the losing write
    assert store.pending_bd_updates() == []  # consumed, not left pending forever

    # A second delivery call must not re-attempt it.
    beads.calls.clear()
    deliver_pending(store, beads)
    assert beads.calls == []


# -- transient failure: left pending, retried ---------------------------------


def test_a_transient_failure_leaves_the_row_pending_and_retries_next_call(store: Store, beads: FakeBeadsClient):
    beads.fail_next("set_metadata")
    store.enqueue_bd_metadata("b-1", {"alloy_stage": "finished"})

    first = deliver_pending(store, beads)
    assert not first[0].ok and first[0].error

    pending = store.pending_bd_updates()
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
    assert pending[0]["last_error"]

    second = deliver_pending(store, beads)
    assert second[0].ok
    assert beads.metadata["b-1"] == {"alloy_stage": "finished"}
    assert store.pending_bd_updates() == []


# -- redelivery of an already-applied row is idempotent -----------------------


def test_redelivering_an_already_delivered_row_is_a_noop(store: Store, beads: FakeBeadsClient):
    beads.statuses["b-1"] = "implementing"
    outbox_id = store.enqueue_bd_status("b-1", "closed", if_status="implementing")
    deliver_pending(store, beads)
    assert beads.statuses["b-1"] == "closed"

    # Marking an already-delivered row delivered again (e.g. a delivery loop
    # that double-processes a stale snapshot) must not re-call bd.
    store.mark_bd_delivered(outbox_id)
    beads.calls.clear()
    deliver_pending(store, beads)

    assert beads.calls == []
    assert beads.statuses["b-1"] == "closed"
