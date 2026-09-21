"""Store-level tests for the execution-monitor data layer (alloy-626).

Covers: `inflight_calls` tracking, `Store.finish_call`'s one-transaction
guarantee, `Store.reconcile_inflight`, `agent_calls.structured_json`, usage
aggregation (`token_totals`/`token_totals_by_role`), repo-scoped
`active_runs`/`all_runs`, and `Store.run_status_totals`.

None of this calls a real model or a real CLI harness -- everything here talks
directly to a throwaway sqlite file via `Store`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from alloy.models import AgentResult, utcnow
from alloy.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "alloy.db")


def _make_run(store: Store, run_id: str, *, repo: Path | str = "/repo", pid: int | None = None) -> None:
    store.create_run(
        run_id=run_id, bead_id=f"bead-{run_id}", thread_id=run_id, recipe="tdd-loop",
        repo=Path(repo), worktree=None, branch=None, log_dir=None,
    )
    if pid is not None:
        store.update_run(run_id, pid=pid)


def _insert_inflight(
    store: Store, call_id: str, run_id: str, *,
    bead_id: str | None = None, role: str = "implement", runner: str = "codex",
    model: str | None = None,
) -> None:
    """Directly populates the `inflight_calls` table -- the schema this bead adds --
    without assuming a particular name for whatever method `RunContext.call` uses
    to insert it; that insertion path is covered separately in test_runtime.py."""
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO inflight_calls (call_id, run_id, bead_id, role, runner, model, started_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (call_id, run_id, bead_id or f"bead-{run_id}", role, runner, model, utcnow().isoformat()),
        )


def _agent_result(*, structured: dict | None = None, usage: dict | None = None) -> AgentResult:
    now = utcnow()
    return AgentResult(
        runner="codex", model=None, ok=True, exit_code=0, text="done",
        structured=structured, started_at=now, ended_at=now, duration_s=1.0,
        usage=usage or {}, log_path="/tmp/log", prompt_hash="deadbeef",
    )


def _finish(store: Store, run_id: str, call_id: str, *, role: str, usage: dict) -> None:
    _insert_inflight(store, call_id, run_id, role=role)
    store.finish_call(
        call_id, run_id=run_id, bead_id=f"bead-{run_id}", role=role,
        iteration=0, result=_agent_result(usage=usage),
    )


def dead_pid() -> int:
    """The pid of a process that has exited and been reaped."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    return process.pid


# -- schema -------------------------------------------------------------


def test_inflight_calls_table_exists_with_call_id_primary_key_and_run_id_index(store: Store):
    with store.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(inflight_calls)")}
        pk_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(inflight_calls)") if row["pk"]
        }
        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(inflight_calls)")
        }
    assert {"call_id", "run_id", "bead_id", "role", "runner", "model", "started_at"} <= columns
    assert pk_columns == {"call_id"}
    assert indexes  # at least the run_id index from the plan doc


def test_agent_calls_has_a_nullable_structured_json_column(store: Store):
    with store.connect() as conn:
        info = {row["name"]: row for row in conn.execute("PRAGMA table_info(agent_calls)")}
    assert "structured_json" in info
    assert info["structured_json"]["notnull"] == 0


# -- active_calls ---------------------------------------------------------


def test_active_calls_filters_by_run_id(store: Store):
    _make_run(store, "run-1")
    _make_run(store, "run-2")
    _insert_inflight(store, "call-1", "run-1")
    _insert_inflight(store, "call-2", "run-1")
    _insert_inflight(store, "call-3", "run-2")

    calls = store.active_calls("run-1")

    assert {c["call_id"] for c in calls} == {"call-1", "call-2"}


def test_active_calls_with_no_run_id_returns_everything(store: Store):
    _make_run(store, "run-1")
    _make_run(store, "run-2")
    _insert_inflight(store, "call-1", "run-1")
    _insert_inflight(store, "call-2", "run-2")

    assert {c["call_id"] for c in store.active_calls()} == {"call-1", "call-2"}


def test_active_calls_is_empty_when_nothing_is_in_flight(store: Store):
    assert store.active_calls() == []
    _make_run(store, "run-1")
    assert store.active_calls("run-1") == []


# -- finish_call ------------------------------------------------------------


def test_finish_call_deletes_the_inflight_row_and_inserts_an_agent_calls_row(store: Store):
    _make_run(store, "run-1")
    _insert_inflight(store, "call-1", "run-1", role="implement", runner="codex")

    store.finish_call(
        "call-1", run_id="run-1", bead_id="bead-run-1", role="implement",
        iteration=2, result=_agent_result(),
    )

    assert store.active_calls("run-1") == []
    calls = store.agent_calls("run-1")
    assert len(calls) == 1
    assert calls[0]["role"] == "implement"
    assert calls[0]["iteration"] == 2
    assert store.get_run("run-1")["agent_calls"] == 1


def test_finish_call_writes_both_statements_over_a_single_connection(store: Store, monkeypatch):
    """The plan requires the delete-from-inflight and insert-into-agent_calls to
    happen in one transaction, closing the polling-window gap a two-connection
    implementation would leave. Assert this structurally: exactly one call to
    `store.connect()` is made by `finish_call`."""
    _make_run(store, "run-1")
    _insert_inflight(store, "call-1", "run-1")

    connect_invocations = []
    real_connect = store.connect

    @contextmanager
    def counting_connect():
        connect_invocations.append(1)
        with real_connect() as conn:
            yield conn

    monkeypatch.setattr(store, "connect", counting_connect)

    store.finish_call(
        "call-1", run_id="run-1", bead_id="bead-run-1", role="implement",
        iteration=0, result=_agent_result(),
    )

    assert len(connect_invocations) == 1


def test_structured_json_round_trips_a_judges_confidence(store: Store):
    _make_run(store, "run-1")
    _insert_inflight(store, "call-1", "run-1", role="judge")
    decision = {"decision": "retry", "reason": "flaky test", "next_instructions": "", "confidence": 0.42}

    store.finish_call(
        "call-1", run_id="run-1", bead_id="bead-run-1", role="judge",
        iteration=0, result=_agent_result(structured=decision),
    )

    row = store.agent_calls("run-1")[0]
    assert json.loads(row["structured_json"]) == decision


def test_structured_json_is_null_when_the_call_had_no_structured_output(store: Store):
    _make_run(store, "run-1")
    _insert_inflight(store, "call-1", "run-1")

    store.finish_call(
        "call-1", run_id="run-1", bead_id="bead-run-1", role="implement",
        iteration=0, result=_agent_result(structured=None),
    )

    row = store.agent_calls("run-1")[0]
    assert row["structured_json"] is None


# -- reconcile_inflight -------------------------------------------------


def test_reconcile_inflight_removes_rows_for_dead_pid_runs_and_keeps_live_ones(store: Store):
    _make_run(store, "dead-run", pid=dead_pid())
    _make_run(store, "live-run")  # create_run defaults pid to this test process, which is alive
    _insert_inflight(store, "call-dead", "dead-run")
    _insert_inflight(store, "call-live", "live-run")

    removed = store.reconcile_inflight()

    assert {c["call_id"] for c in removed} == {"call-dead"}
    assert {c["call_id"] for c in store.active_calls()} == {"call-live"}


def test_reconcile_inflight_is_a_no_op_when_nothing_is_stale(store: Store):
    _make_run(store, "live-run")
    _insert_inflight(store, "call-live", "live-run")

    assert store.reconcile_inflight() == []
    assert {c["call_id"] for c in store.active_calls()} == {"call-live"}


# -- usage aggregation ----------------------------------------------------


def test_token_totals_sums_normalized_usage_including_a_totals_only_shape(store: Store):
    _make_run(store, "run-1")
    _finish(store, "run-1", "call-1", role="implement", usage={"input_tokens": 100, "output_tokens": 20})
    _finish(store, "run-1", "call-2", role="implement", usage={"inputTokens": 50, "outputTokens": 5})
    _finish(store, "run-1", "call-3", role="judge", usage={"input_tokens": 200})  # totals-only shape

    totals = store.token_totals("run-1")

    assert totals["input_tokens"] == 150   # totals-only record contributes 0, not None, to the sum
    assert totals["output_tokens"] == 25
    assert totals["total_tokens"] == 120 + 55 + 200


def test_token_totals_by_role_keeps_roles_separate(store: Store):
    _make_run(store, "run-1")
    _finish(store, "run-1", "call-1", role="implement", usage={"input_tokens": 100, "output_tokens": 20})
    _finish(store, "run-1", "call-2", role="implement", usage={"inputTokens": 50, "outputTokens": 5})
    _finish(store, "run-1", "call-3", role="judge", usage={"input_tokens": 200})

    by_role = store.token_totals_by_role("run-1")

    assert by_role["implement"]["input_tokens"] == 150
    assert by_role["implement"]["output_tokens"] == 25
    assert by_role["implement"]["total_tokens"] == 175
    assert by_role["judge"]["input_tokens"] == 0
    assert by_role["judge"]["output_tokens"] == 0
    assert by_role["judge"]["total_tokens"] == 200


def test_token_totals_for_a_run_with_no_calls_is_all_zero(store: Store):
    _make_run(store, "run-1")

    totals = store.token_totals("run-1")

    assert totals["input_tokens"] == 0
    assert totals["output_tokens"] == 0
    assert totals["total_tokens"] == 0


def test_token_totals_cost_usd_is_none_when_no_call_reported_a_cost(store: Store):
    _make_run(store, "run-1")
    _finish(store, "run-1", "call-1", role="implement", usage={"input_tokens": 100, "output_tokens": 20})
    _finish(store, "run-1", "call-2", role="judge", usage={"input_tokens": 200})

    assert store.token_totals("run-1")["cost_usd"] is None
    assert store.token_totals_by_role("run-1")["judge"]["cost_usd"] is None


def test_token_totals_cost_usd_sums_only_the_reported_costs(store: Store):
    _make_run(store, "run-1")
    _finish(store, "run-1", "call-1", role="implement",
            usage={"input_tokens": 100, "output_tokens": 20, "total_cost_usd": 0.25})
    _finish(store, "run-1", "call-2", role="implement", usage={"input_tokens": 50, "output_tokens": 5})
    _finish(store, "run-1", "call-3", role="judge",
            usage={"input_tokens": 10, "output_tokens": 1, "total_cost_usd": 0.5})

    by_role = store.token_totals_by_role("run-1")

    assert store.token_totals("run-1")["cost_usd"] == pytest.approx(0.75)
    assert by_role["implement"]["cost_usd"] == pytest.approx(0.25)
    assert by_role["judge"]["cost_usd"] == pytest.approx(0.5)


# -- repo scoping and lifetime totals -------------------------------------


def test_active_runs_filters_by_repo(store: Store, tmp_path: Path):
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    _make_run(store, "run-a", repo=repo_a)
    _make_run(store, "run-b", repo=repo_b)

    assert [r["run_id"] for r in store.active_runs(repo=repo_a)] == ["run-a"]
    assert [r["run_id"] for r in store.active_runs(repo=repo_b)] == ["run-b"]


def test_active_runs_with_no_repo_preserves_current_global_behaviour(store: Store, tmp_path: Path):
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    _make_run(store, "run-a", repo=repo_a)
    _make_run(store, "run-b", repo=repo_b)

    assert {r["run_id"] for r in store.active_runs()} == {"run-a", "run-b"}


def test_all_runs_filters_by_repo(store: Store, tmp_path: Path):
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    _make_run(store, "run-a", repo=repo_a)
    _make_run(store, "run-b", repo=repo_b)

    assert [r["run_id"] for r in store.all_runs(repo=repo_a)] == ["run-a"]


def test_all_runs_with_no_repo_preserves_current_global_behaviour(store: Store, tmp_path: Path):
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    _make_run(store, "run-a", repo=repo_a)
    _make_run(store, "run-b", repo=repo_b)

    assert {r["run_id"] for r in store.all_runs()} == {"run-a", "run-b"}


def test_run_status_totals_counts_the_whole_table_not_a_default_page(store: Store, tmp_path: Path):
    repo = tmp_path / "repo"
    for i in range(150):  # bigger than all_runs' default limit=100
        run_id = f"run-{i}"
        _make_run(store, run_id, repo=repo)
        store.finish_run(run_id, status="done", outcome="done")

    other_repo = tmp_path / "other-repo"
    _make_run(store, "other-run", repo=other_repo)
    store.finish_run("other-run", status="failed", outcome="failed")

    totals = store.run_status_totals(repo)

    assert totals == {"done": 150}


def test_run_status_totals_is_scoped_to_the_given_repo(store: Store, tmp_path: Path):
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    _make_run(store, "a-done", repo=repo_a)
    store.finish_run("a-done", status="done", outcome="done")
    _make_run(store, "b-failed", repo=repo_b)
    store.finish_run("b-failed", status="failed", outcome="failed")

    assert store.run_status_totals(repo_a) == {"done": 1}
    assert store.run_status_totals(repo_b) == {"failed": 1}


# -- schema evolution -------------------------------------------------------


def test_opening_store_against_a_pre_existing_db_without_the_new_schema_does_not_fail(tmp_path: Path):
    """Simulates an `alloy.db` created before this bead: `agent_calls` exists but
    has no `structured_json` column, and `inflight_calls` does not exist at all.
    Opening it with the upgraded `Store` must not raise, and the new columns/
    tables must actually be usable afterward."""
    import sqlite3

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE runs (
            run_id         TEXT PRIMARY KEY,
            bead_id        TEXT NOT NULL,
            thread_id      TEXT NOT NULL,
            recipe         TEXT NOT NULL,
            repo           TEXT NOT NULL,
            worktree       TEXT,
            branch         TEXT,
            status         TEXT NOT NULL,
            stage          TEXT,
            iteration      INTEGER NOT NULL DEFAULT 0,
            consiliums     INTEGER NOT NULL DEFAULT 0,
            agent_calls    INTEGER NOT NULL DEFAULT 0,
            tests_summary  TEXT,
            started_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            ended_at       TEXT,
            outcome        TEXT,
            outcome_reason TEXT,
            log_dir        TEXT,
            pid            INTEGER
        );
        CREATE TABLE agent_calls (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       TEXT NOT NULL,
            bead_id      TEXT NOT NULL,
            role         TEXT NOT NULL,
            runner       TEXT NOT NULL,
            model        TEXT,
            prompt_hash  TEXT NOT NULL,
            started_at   TEXT NOT NULL,
            ended_at     TEXT NOT NULL,
            duration_s   REAL NOT NULL,
            exit_code    INTEGER NOT NULL,
            ok           INTEGER NOT NULL,
            usage_json   TEXT NOT NULL DEFAULT '{}',
            log_path     TEXT,
            iteration    INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()

    store = Store(db_path)  # must not raise on an old, column-missing schema

    _make_run(store, "r1", repo=tmp_path)
    _insert_inflight(store, "call-1", "r1")
    store.finish_call(
        "call-1", run_id="r1", bead_id="bead-r1", role="implement",
        iteration=0, result=_agent_result(structured={"decision": "done"}),
    )

    row = store.agent_calls("r1")[0]
    assert json.loads(row["structured_json"]) == {"decision": "done"}
