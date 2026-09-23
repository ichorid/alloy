"""Beads is the source of truth: readiness in, execution status out.

These run against the real `bd` CLI, because the contract that matters here is
its exit codes and JSON, not a mock of them.
"""

from __future__ import annotations

import pytest

from alloy import beads as bd
from alloy.beads import BeadsClient, BeadsError
from conftest import bd_create


def _show_json(client: BeadsClient, bead_id: str) -> dict:
    rows = client._json(["show", bead_id])
    assert rows, f"bead {bead_id} not found"
    return rows[0]


def _bug_ids(client: BeadsClient) -> list[str]:
    return [row["id"] for row in client._json(["list", "--type", "bug", "--limit", "0", "--flat"])]


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


def test_check_hint_is_read_from_the_bead(client, beads_project):
    bead_id = bd_create(beads_project, "task", alloy_recipe="tdd-loop",
                        alloy_test_cmd="make check")
    assert client.show(bead_id).check_hint == "make check"


# -- bug filing (create_bug / add_dependency) -------------------------------


def test_create_bug_returns_a_properly_filed_bug_bead(client, beads_project):
    parent_id = bd_create(
        beads_project,
        "parent task",
        priority=1,
        alloy_recipe="tdd-loop",
        alloy_test_cmd="make check",
    )
    run_id = "run-abc123"
    acceptance = "pytest -q tests/regression passes"

    bug_id = client.create_bug(
        title="slugify mishandles combining marks",
        description="Evidence: test_slugify_unicode failed with 'café' != 'cafe'",
        acceptance=acceptance,
        discovered_from=parent_id,
        priority=3,
        labels=[bd.LABEL_BUG],
        metadata={
            bd.META_RECIPE: "tdd-loop",
            bd.META_TEST_CMD: "make check",
            bd.META_DISCOVERED_IN_RUN: run_id,
        },
    )

    bug = client.show(bug_id)
    assert bug.issue_type == "bug"
    assert bug.priority == 3
    assert bd.LABEL_BUG in bug.labels
    assert bug.acceptance_criteria == acceptance
    assert bug.metadata[bd.META_RECIPE] == "tdd-loop"
    assert bug.metadata[bd.META_DISCOVERED_IN_RUN] == run_id

    deps = _show_json(client, bug_id).get("dependencies") or []
    assert any(
        dep.get("id") == parent_id and dep.get("dependency_type") == "discovered-from"
        for dep in deps
    )

    ready_ids = [bead.id for bead in client.ready()]
    assert parent_id in ready_ids


def test_create_bug_with_claim_is_implementing_and_not_ready(client, beads_project):
    parent_id = bd_create(beads_project, "parent", alloy_recipe="tdd-loop")

    bug_id = client.create_bug(
        title="race in claim path",
        description="found while implementing parent",
        acceptance="fixed",
        discovered_from=parent_id,
        priority=2,
        labels=[bd.LABEL_BUG],
        metadata={
            bd.META_RECIPE: "tdd-loop",
            bd.META_DISCOVERED_IN_RUN: "run-claim",
        },
        claim=True,
    )

    assert client.show(bug_id).status == bd.STATUS_IMPLEMENTING
    assert bug_id not in [bead.id for bead in client.ready()]


def test_create_bug_with_human_label_is_listed_by_bd_human(client, beads_project):
    parent_id = bd_create(beads_project, "parent", alloy_recipe="tdd-loop")

    bug_id = client.create_bug(
        title="needs architecture decision",
        description="cannot fix without API change",
        acceptance="human resolved and tests pass",
        discovered_from=parent_id,
        priority=1,
        labels=[bd.LABEL_BUG, bd.LABEL_HUMAN],
        metadata={
            bd.META_RECIPE: "tdd-loop",
            bd.META_DISCOVERED_IN_RUN: "run-human",
        },
    )

    human_ids = [row["id"] for row in client._json(["human", "list"])]
    assert bug_id in human_ids


def test_add_dependency_blocks_parent_until_bug_is_closed(client, beads_project):
    parent_id = bd_create(beads_project, "parent", alloy_recipe="tdd-loop")
    bug_id = client.create_bug(
        title="blocking defect",
        description="parent cannot proceed",
        acceptance="regression test green",
        discovered_from=parent_id,
        priority=2,
        labels=[bd.LABEL_BUG],
        metadata={
            bd.META_RECIPE: "tdd-loop",
            bd.META_DISCOVERED_IN_RUN: "run-block",
        },
    )

    client.add_dependency(parent_id, bug_id)

    assert parent_id not in [bead.id for bead in client.ready()]

    client.close(bug_id)

    assert parent_id in [bead.id for bead in client.ready()]


def test_create_bug_rejects_priority_zero_without_creating_a_bead(client, beads_project):
    parent_id = bd_create(beads_project, "parent", alloy_recipe="tdd-loop")
    before = set(_bug_ids(client))

    with pytest.raises(ValueError, match="priority"):
        client.create_bug(
            title="must not outrank humans",
            description="agent tried P0",
            acceptance="n/a",
            discovered_from=parent_id,
            priority=0,
            labels=[bd.LABEL_BUG],
            metadata={
                bd.META_RECIPE: "tdd-loop",
                bd.META_DISCOVERED_IN_RUN: "run-p0",
            },
        )

    assert set(_bug_ids(client)) == before


def _create_epic(client: BeadsClient, title: str, description: str) -> str:
    proc = client._run(["create", title, "-t", "epic", "-d", description, "--silent"])
    return proc.stdout.strip().splitlines()[-1]


def _create_child(client: BeadsClient, title: str, parent_id: str) -> str:
    proc = client._run(["create", title, "--parent", parent_id, "--silent"])
    return proc.stdout.strip().splitlines()[-1]


@pytest.fixture
def snapshot_graph(client, beads_project):
    """Epic with two open children, one implementing child, and one alloy-bug."""
    epic_id = _create_epic(client, "OAuth login", "Ship OAuth for the API")
    open_a = _create_child(client, "add token endpoint", epic_id)
    open_b = _create_child(client, "wire callback route", epic_id)
    implementing = _create_child(client, "session middleware", epic_id)
    client.set_status(implementing, bd.STATUS_IMPLEMENTING)
    parent_for_bug = bd_create(beads_project, "running parent", alloy_recipe="tdd-loop")
    bug_id = client.create_bug(
        title="stale refresh token",
        description="tokens never rotate",
        acceptance="refresh rotates tokens",
        discovered_from=parent_for_bug,
        priority=3,
        labels=[bd.LABEL_BUG],
        metadata={bd.META_RECIPE: "tdd-loop", bd.META_DISCOVERED_IN_RUN: "run-snap"},
    )
    return {
        "epic_id": epic_id,
        "child_id": open_a,
        "open_a": open_a,
        "open_b": open_b,
        "implementing": implementing,
        "bug_id": bug_id,
    }


def test_project_snapshot_lists_epic_open_implementing_and_filed_bugs(
    client, snapshot_graph,
):
    snapshot = client.project_snapshot(snapshot_graph["child_id"])

    assert "OAuth login" in snapshot.epic
    assert "Ship OAuth for the API" in snapshot.epic

    open_text = "\n".join(snapshot.open_beads)
    assert f"{snapshot_graph['open_a']}" in open_text
    assert "add token endpoint" in open_text
    assert f"{snapshot_graph['open_b']}" in open_text
    assert "wire callback route" in open_text
    assert f"{snapshot_graph['implementing']}" in open_text
    assert "session middleware" in open_text

    filed_text = "\n".join(snapshot.filed_bugs)
    assert snapshot_graph["bug_id"] in filed_text
    assert "stale refresh token" in filed_text

    assert snapshot.stats.get("open_issues", 0) >= 1
    assert "closed_issues" in snapshot.stats


def test_project_snapshot_degrades_gracefully_when_bd_missing(beads_project):
    client = BeadsClient(repo=beads_project, binary="/nonexistent/bd")

    snapshot = client.project_snapshot("t-1")

    assert snapshot.open_beads == []
    assert snapshot.epic == ""
    assert snapshot.filed_bugs == []
    assert snapshot.stats == {}


def test_create_bug_invalid_priority_string_raises_beads_error(client, beads_project):
    parent_id = bd_create(beads_project, "parent", alloy_recipe="tdd-loop")

    with pytest.raises(BeadsError):
        client.create_bug(
            title="bad priority",
            description="bd should reject this",
            acceptance="n/a",
            discovered_from=parent_id,
            priority="not-a-valid-priority",
            labels=[bd.LABEL_BUG],
            metadata={
                bd.META_RECIPE: "tdd-loop",
                bd.META_DISCOVERED_IN_RUN: "run-bad-priority",
            },
        )


# -- fake-bd read helpers (alloy-byo.1) ------------------------------------


@pytest.fixture
def read_client(fake_bd):
    return BeadsClient(repo=fake_bd.repo, binary=str(fake_bd.binary))


def test_blocked_returns_beads_with_blocked_by(read_client, fake_bd):
    fake_bd.configure({
        "blocked": [
            {
                "id": "beads-1",
                "title": "blocked task",
                "status": "open",
                "blocked_by": ["a", "b"],
            },
        ],
    })

    beads = read_client.blocked()

    assert len(beads) == 1
    assert beads[0].id == "beads-1"
    assert beads[0].blocked_by == ["a", "b"]


def test_children_passes_parent_and_all_and_returns_open_and_closed(read_client, fake_bd):
    fake_bd.configure({
        "beads": [
            {"id": "child-open", "title": "open child", "status": "open", "parent": "E"},
            {"id": "child-closed", "title": "closed child", "status": "closed", "parent": "E"},
            {"id": "other", "title": "other parent", "status": "open", "parent": "F"},
        ],
    })

    children = read_client.children("E")

    assert {bead.id for bead in children} == {"child-open", "child-closed"}
    list_calls = [call for call in fake_bd.calls if call["command"] == "list"]
    assert list_calls, "children() should invoke bd list"
    assert any(
        "--parent" in call["argv"]
        and "E" in call["argv"]
        and "--all" in call["argv"]
        for call in list_calls
    )


def test_epic_for_returns_none_for_bead_without_parent(read_client, fake_bd):
    fake_bd.configure({
        "shows": {
            "orphan": {"id": "orphan", "title": "root task", "issue_type": "task"},
        },
    })

    assert read_client.epic_for("orphan") is None


def test_epic_for_returns_epic_id_for_grandchild(read_client, fake_bd):
    fake_bd.configure({
        "shows": {
            "epic-1": {"id": "epic-1", "title": "Big Epic", "issue_type": "epic"},
            "child-1": {
                "id": "child-1",
                "title": "Child",
                "issue_type": "task",
                "parent": "epic-1",
            },
            "grand-1": {
                "id": "grand-1",
                "title": "Grandchild",
                "issue_type": "task",
                "parent": "child-1",
            },
        },
    })

    assert read_client.epic_for("grand-1") == "epic-1"
