"""Alloy's own metadata: which run belongs to which bead, and what every agent
call cost.

This is the table that makes the later questions answerable -- "can this resume
after a reboot?", and eventually "which harness is actually better at this role?"
LangGraph owns graph state; this owns the index over runs.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from alloy.models import AgentCallRecord, AgentResult, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
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
CREATE INDEX IF NOT EXISTS runs_bead_idx ON runs(bead_id);
CREATE INDEX IF NOT EXISTS runs_status_idx ON runs(status);

CREATE TABLE IF NOT EXISTS agent_calls (
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
CREATE INDEX IF NOT EXISTS agent_calls_run_idx ON agent_calls(run_id);
"""

RUN_RUNNING = "running"
RUN_WAITING_HUMAN = "waiting-human"
RUN_DONE = "done"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"

TERMINAL_RUN_STATUSES = {RUN_DONE, RUN_FAILED, RUN_CANCELLED}


@dataclass
class Store:
    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- runs -------------------------------------------------------------

    def create_run(
        self,
        *,
        run_id: str,
        bead_id: str,
        thread_id: str,
        recipe: str,
        repo: Path,
        worktree: Path | None,
        branch: str | None,
        log_dir: Path | None,
    ) -> None:
        now = utcnow().isoformat()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, bead_id, thread_id, recipe, repo, worktree, branch,"
                " status, stage, started_at, updated_at, log_dir, pid)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, bead_id, thread_id, recipe, str(repo),
                    str(worktree) if worktree else None, branch,
                    RUN_RUNNING, "starting", now, now,
                    str(log_dir) if log_dir else None, os.getpid(),
                ),
            )

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utcnow().isoformat()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                (*fields.values(), run_id),
            )

    def finish_run(self, run_id: str, *, status: str, outcome: str, reason: str = "") -> None:
        self.update_run(
            run_id,
            status=status,
            outcome=outcome,
            outcome_reason=reason,
            ended_at=utcnow().isoformat(),
            pid=None,
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def latest_run_for_bead(self, bead_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE bead_id = ? ORDER BY started_at DESC LIMIT 1",
                (bead_id,),
            ).fetchone()
        return dict(row) if row else None

    def active_runs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status NOT IN (?,?,?) ORDER BY started_at",
                (RUN_DONE, RUN_FAILED, RUN_CANCELLED),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def orphaned_runs(self) -> list[dict[str, Any]]:
        """Runs marked running whose process is gone -- i.e. crash survivors."""
        return [run for run in self.active_runs()
                if run["status"] == RUN_RUNNING and not _pid_alive(run["pid"])]

    # -- agent call ledger ------------------------------------------------

    def record_agent_call(
        self, *, run_id: str, bead_id: str, role: str, iteration: int, result: AgentResult
    ) -> None:
        record = AgentCallRecord(
            run_id=run_id,
            bead_id=bead_id,
            role=role,
            runner=result.runner,
            model=result.model,
            prompt_hash=result.prompt_hash,
            started_at=result.started_at,
            ended_at=result.ended_at,
            duration_s=result.duration_s,
            exit_code=result.exit_code,
            ok=result.ok,
            usage_json=json.dumps(result.usage),
            log_path=result.log_path,
            iteration=iteration,
        )
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO agent_calls (run_id, bead_id, role, runner, model, prompt_hash,"
                " started_at, ended_at, duration_s, exit_code, ok, usage_json, log_path, iteration)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.run_id, record.bead_id, record.role, record.runner, record.model,
                    record.prompt_hash, _iso(record.started_at), _iso(record.ended_at),
                    record.duration_s, record.exit_code, int(record.ok),
                    record.usage_json, record.log_path, record.iteration,
                ),
            )
            conn.execute(
                "UPDATE runs SET agent_calls = agent_calls + 1 WHERE run_id = ?", (run_id,)
            )

    def agent_calls(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_calls WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def call_count(self, run_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM agent_calls WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row["n"]) if row else 0


def _iso(value: datetime | str) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True
