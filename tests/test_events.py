"""The attention feed: `events.jsonl`, `alloy events`, and stall detection."""

from __future__ import annotations

import json
import os
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alloy.cli import app
from alloy.events import ATTENTION_EVENTS, EventLog, format_line, parse_since
from alloy.models import utcnow
from alloy.scheduler import Scheduler
from alloy.store import RUN_DONE, RUN_FAILED, RUN_RUNNING, RUN_WAITING_HUMAN, Store


class _Engine:
    """The slice of Engine that check_stalls touches."""

    def __init__(self, store: Store, repo: Path) -> None:
        self.store, self.repo = store, repo


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "alloy.db")


def _run(store: Store, run_id: str = "r1", bead: str = "b-1") -> str:
    store.create_run(run_id=run_id, bead_id=bead, thread_id=run_id, recipe="tdd-loop",
                     repo=Path("/repo"), worktree=None, branch=None, log_dir=None)
    return run_id


def _kinds(store: Store) -> list[str]:
    return [e["event"] for e in store.events.read()]


def test_emit_appends_one_json_line_per_event(tmp_path):
    feed = EventLog(tmp_path / "sub" / "events.jsonl")
    feed.emit("needs-human", bead="b-1", run="r1", reason="pick a schema")
    feed.emit("done", bead="b-2")
    lines = feed.path.read_text().splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["needs-human", "done"]
    assert json.loads(lines[0])["reason"] == "pick a schema"


def test_read_filters_by_kind_and_time_and_skips_garbage(tmp_path):
    feed = EventLog(tmp_path / "events.jsonl")
    feed.emit("failed", bead="b-1")
    feed.emit("done", bead="b-2")
    with open(feed.path, "a") as handle:
        handle.write("not json\n")
    assert [e["bead"] for e in feed.read(only=frozenset({"failed"}))] == ["b-1"]
    assert len(feed.read(since=utcnow() - timedelta(minutes=1))) == 2
    assert feed.read(since=utcnow() + timedelta(minutes=1)) == []


def test_parse_since_accepts_durations_and_iso():
    now = utcnow()
    assert parse_since("30m", now) == now - timedelta(minutes=30)
    assert parse_since("2h", now) == now - timedelta(hours=2)
    assert parse_since("2026-01-02T03:04:05").year == 2026
    with pytest.raises(ValueError):
        parse_since("soon")


def test_follow_skips_history_and_yields_new_events(tmp_path):
    feed = EventLog(tmp_path / "events.jsonl")
    feed.emit("done", bead="old")
    seen: list[str] = []
    ticks = {"n": 0}

    def sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            feed.emit("needs-human", bead="new")

    for record in feed.follow(sleep=sleep, stop=lambda: ticks["n"] >= 3):
        seen.append(record["bead"])
    assert seen == ["new"]


def test_follow_with_since_replays_history_first(tmp_path):
    feed = EventLog(tmp_path / "events.jsonl")
    feed.emit("failed", bead="old")
    got = feed.follow(since=utcnow() - timedelta(hours=1), sleep=lambda _s: None)
    assert next(got)["bead"] == "old"


def test_follow_waits_for_a_file_that_does_not_exist_yet(tmp_path):
    feed = EventLog(tmp_path / "events.jsonl")
    ticks = {"n": 0}

    def sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            feed.emit("stalled", bead="late")

    assert [r["bead"] for r in feed.follow(sleep=sleep, stop=lambda: ticks["n"] >= 3)] == ["late"]


def test_concurrent_writers_never_interleave_lines(tmp_path):
    feed = EventLog(tmp_path / "events.jsonl")

    def write() -> None:
        for _ in range(50):
            feed.emit("done", bead="b", reason="x" * 200)

    threads = [threading.Thread(target=write) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(feed.read()) == 200


def test_format_line_is_a_single_greppable_line():
    line = format_line({"ts": "2026-01-01T00:00:00.5+00:00", "event": "needs-human",
                        "bead": "b-1", "run": "r1", "reason": "two\nlines"})
    assert line == "2026-01-01T00:00:00 needs-human b-1 run=r1 two lines"


def test_store_announces_attention_transitions_once(store):
    run = _run(store)
    store.update_run(run, stage="verify")  # no status change, no event
    store.update_run(run, status=RUN_WAITING_HUMAN, event_reason="need a decision")
    store.update_run(run, status=RUN_WAITING_HUMAN)  # unchanged
    store.update_run(run, status=RUN_RUNNING)
    store.finish_run(run, status=RUN_FAILED, outcome="failed", reason="tests red")
    events = store.events.read()
    assert [e["event"] for e in events] == ["needs-human", "resumed", "failed"]
    assert events[0]["reason"] == "need a decision" and events[0]["bead"] == "b-1"
    assert events[2]["reason"] == "tests red"


def test_done_is_announced_but_not_an_attention_event(store):
    run = _run(store)
    store.finish_run(run, status=RUN_DONE, outcome="done")
    assert _kinds(store) == ["done"]
    assert "done" not in ATTENTION_EVENTS


def _scheduler(store: Store, minutes: float = 30) -> Scheduler:
    return Scheduler(engine=_Engine(store, Path("/repo")), stall_minutes=minutes)  # type: ignore[arg-type]


def _age(store: Store, run_id: str, minutes: float) -> None:
    stamp = (utcnow() - timedelta(minutes=minutes)).isoformat()
    with store.connect() as conn:
        conn.execute("UPDATE runs SET updated_at = ? WHERE run_id = ?", (stamp, run_id))


def test_idle_running_run_is_reported_stalled_exactly_once(store):
    run = _run(store)
    store.update_run(run, pid=os.getpid(), stage="implement")
    scheduler = _scheduler(store)
    assert scheduler.check_stalls() == []
    _age(store, run, 45)
    assert scheduler.check_stalls() == [run]
    assert scheduler.check_stalls() == []
    (event,) = store.events.read()
    assert event["event"] == "stalled" and "45 min" in event["reason"]
    assert "implement" in event["reason"]


def test_a_stalled_run_that_moves_and_stalls_again_is_reported_again(store):
    run = _run(store)
    store.update_run(run, pid=os.getpid())
    scheduler = _scheduler(store)
    _age(store, run, 60)
    scheduler.check_stalls()
    store.update_run(run, stage="verify")  # activity
    assert scheduler.check_stalls() == []
    _age(store, run, 60)
    assert scheduler.check_stalls() == [run]
    assert _kinds(store) == ["stalled", "stalled"]


def test_a_recent_agent_call_counts_as_activity(store):
    run = _run(store)
    store.update_run(run, pid=os.getpid())
    _age(store, run, 60)
    store.start_call("c1", run_id=run, bead_id="b-1", role="implement", runner="x", model=None)
    assert _scheduler(store).check_stalls() == []


def test_a_long_running_agent_call_is_named_in_the_stall_reason(store):
    run = _run(store)
    store.update_run(run, pid=os.getpid())
    _age(store, run, 60)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO inflight_calls (call_id, run_id, bead_id, role, runner, started_at)"
            " VALUES ('c1', ?, 'b-1', 'implement', 'x', ?)",
            (run, (utcnow() - timedelta(minutes=50)).isoformat()),
        )
    assert _scheduler(store).check_stalls() == [run]
    assert "agent call implement" in store.events.read()[0]["reason"]


def test_a_dead_process_is_reported_immediately(store):
    run = _run(store)
    store.update_run(run, pid=2**22 + 1)
    assert _scheduler(store).check_stalls() == [run]
    assert "gone" in store.events.read()[0]["reason"]


def test_waiting_and_finished_runs_are_never_stalled(store):
    parked = _run(store, "r1", "b-1")
    store.update_run(parked, status=RUN_WAITING_HUMAN)
    _age(store, parked, 500)
    assert _scheduler(store).check_stalls() == []


def test_stall_detection_can_be_disabled(store):
    run = _run(store)
    store.update_run(run, pid=os.getpid())
    _age(store, run, 500)
    assert _scheduler(store, minutes=0).check_stalls() == []


def test_cli_events_prints_history_and_filters(tmp_path):
    feed = EventLog(tmp_path / "events.jsonl")
    feed.emit("needs-human", bead="b-1", run="r1", reason="decide")
    feed.emit("done", bead="b-2")
    runner = CliRunner()
    result = runner.invoke(app, ["events", "--root", str(tmp_path), "--attention"])
    assert result.exit_code == 0
    assert "needs-human b-1 run=r1 decide" in result.stdout and "b-2" not in result.stdout
    result = runner.invoke(app, ["events", "--root", str(tmp_path), "--json", "--only", "done"])
    assert json.loads(result.stdout.strip())["bead"] == "b-2"
    result = runner.invoke(app, ["events", "--root", str(tmp_path), "--since", "nope"])
    assert result.exit_code == 1
