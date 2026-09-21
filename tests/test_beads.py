"""Beads is the source of truth: readiness in, execution status out.

These run against the real `bd` CLI, because the contract that matters here is
its exit codes and JSON, not a mock of them.
"""

from __future__ import annotations

import pytest

from alloy import beads as bd
from alloy.beads import BeadsClient
from conftest import bd_create


@pytest.fixture
def client(beads_project):
    return BeadsClient(repo=beads_project)


def test_alloy_statuses_are_registered(client):
    beads = client._run(["statuses"]).stdout
    for status in ("implementing", "waiting-human", "review-ready", "failed"):
        assert status in beads


def test_ready_lists_only_beads_assigned_to_a_recipe(client, beads_project):
    assigned = bd_create(beads_project, "assigned task", alloy_recipe="tdd-loop")
    bd_create(beads_project, "unassigned task")

    ready = client.ready()

    assert [bead.id for bead in ready] == [assigned]
    assert ready[0].recipe == "tdd-loop"


def test_ready_is_ordered_by_priority(client, beads_project):
    low = bd_create(beads_project, "low", priority=3, alloy_recipe="tdd-loop")
    high = bd_create(beads_project, "high", priority=0, alloy_recipe="tdd-loop")
    mid = bd_create(beads_project, "mid", priority=1, alloy_recipe="tdd-loop")

    assert [bead.id for bead in client.ready()] == [high, mid, low]


def test_claiming_moves_ready_to_implementing(client, beads_project):
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")

    assert client.claim(bead_id)
    assert client.show(bead_id).status == bd.STATUS_IMPLEMENTING


def test_a_second_claim_loses_the_race_instead_of_stealing_the_task(client, beads_project):
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")

    assert client.claim(bead_id) is True
    assert client.claim(bead_id) is False  # compare-and-set refused it


def test_claimed_beads_disappear_from_ready(client, beads_project):
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")
    client.claim(bead_id)

    assert client.ready() == []


@pytest.mark.parametrize(
    "status",
    [bd.STATUS_WAITING_HUMAN, bd.STATUS_REVIEW_READY, bd.STATUS_FAILED],
)
def test_each_execution_status_round_trips(client, beads_project, status):
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")
    client.claim(bead_id)

    assert client.set_status(bead_id, status)
    assert client.show(bead_id).status == status


def test_status_guard_refuses_a_stale_transition(client, beads_project):
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")
    client.claim(bead_id)

    assert client.set_status(bead_id, bd.STATUS_FAILED, if_status=bd.STATUS_READY) is False
    assert client.show(bead_id).status == bd.STATUS_IMPLEMENTING


def test_alloy_metadata_points_back_at_the_run(client, beads_project):
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop")
    client.set_metadata(bead_id, {
        bd.META_RUN_ID: "abc123",
        bd.META_WORKTREE: "/tmp/wt",
        bd.META_BRANCH: "alloy/t-1",
        bd.META_STAGE: "implement",
    })

    metadata = client.show(bead_id).metadata
    assert metadata[bd.META_RUN_ID] == "abc123"
    assert metadata[bd.META_STAGE] == "implement"


def test_task_brief_carries_the_human_authored_fields(client, beads_project):
    bead_id = bd_create(beads_project, "add slugify", alloy_recipe="tdd-loop")
    client._run(["update", bead_id, "-d", "Add slugify() to mypkg",
                 "--design", "use a regex", "--acceptance", "handles unicode"])

    bead = client.show(bead_id)
    brief = bead.task_brief()

    assert "add slugify" in brief
    assert "Add slugify() to mypkg" in brief
    assert "use a regex" in brief
    assert bead.acceptance_criteria == "handles unicode"


def test_a_blocked_bead_is_not_ready(client, beads_project):
    blocker = bd_create(beads_project, "blocker", alloy_recipe="tdd-loop")
    blocked = bd_create(beads_project, "blocked", alloy_recipe="tdd-loop")
    client._run(["dep", "add", blocked, blocker])

    assert blocked not in [bead.id for bead in client.ready()]


def test_test_command_override_is_read_from_the_bead(client, beads_project):
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop",
                        alloy_test_cmd="make check")
    assert client.show(bead_id).test_command == "make check"
