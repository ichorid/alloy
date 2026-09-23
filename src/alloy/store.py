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
from typing import Any, Iterable, Iterator

from alloy.limits import harness_for_runner
from alloy.models import AgentCallRecord, AgentResult, utcnow
from alloy.usage import normalize

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
    prefix_hash  TEXT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT NOT NULL,
    duration_s   REAL NOT NULL,
    exit_code    INTEGER NOT NULL,
    ok           INTEGER NOT NULL,
    usage_json   TEXT NOT NULL DEFAULT '{}',
    structured_json TEXT,
    log_path     TEXT,
    iteration    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS agent_calls_run_idx ON agent_calls(run_id);

CREATE TABLE IF NOT EXISTS inflight_calls (
    call_id      TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    bead_id      TEXT NOT NULL,
    role         TEXT NOT NULL,
    runner       TEXT NOT NULL,
    model        TEXT,
    started_at   TEXT NOT NULL,
    pid          INTEGER
);
CREATE INDEX IF NOT EXISTS inflight_calls_run_idx ON inflight_calls(run_id);

CREATE TABLE IF NOT EXISTS scheduler_meta (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL
);
"""

# scheduler_meta keys (alloy-4ef.19)
META_FINISHED_RUNS_SINCE_REVIEW = "finished_runs_since_last_review"
META_LAST_MEMORY_REVIEW_DAY = "last_memory_review_day"

RUN_RUNNING = "running"
RUN_WAITING_HUMAN = "waiting-human"
RUN_DONE = "done"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"

TERMINAL_RUN_STATUSES = {RUN_DONE, RUN_FAILED, RUN_CANCELLED}

# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` never
# alters an existing table, so each is added on open when missing. Keep this
# additive: a column is never renamed or dropped here.
MIGRATIONS: dict[str, dict[str, str]] = {
    "runs": {
        "complexity": "TEXT",
        "paused_at": "TEXT",                       # set while waiting for a human
        "paused_s": "REAL NOT NULL DEFAULT 0",     # total time spent paused
        "parent_run_id": "TEXT",                   # set on a remediation child run (alloy-0uc.8)
        "dispatch_tier": "TEXT",                   # tier used by live routing (alloy-0uc.3); None in shadow
        "escalations": "INTEGER DEFAULT 0",
        "retry_at": "TEXT",                        # when a parked run may resume on its own (alloy-5wb.4)
    },
    "agent_calls": {
        "structured_json": "TEXT",                 # the raw structured output, e.g. the judge's verdict
        "prefix_hash": "TEXT",                     # sha256 of the prompt's stable layers (alloy-4ef.6)
    },
    "inflight_calls": {
        "pid": "INTEGER",                          # the harness process group, so a dead run's orphan can be killed
    },
}


@dataclass
class Store:
    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            _ensure_columns(conn, MIGRATIONS)

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
        parent_run_id: str | None = None,
    ) -> None:
        now = utcnow().isoformat()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, bead_id, thread_id, recipe, repo, worktree, branch,"
                " status, stage, started_at, updated_at, log_dir, pid, parent_run_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, bead_id, thread_id, recipe, str(repo),
                    str(worktree) if worktree else None, branch,
                    RUN_RUNNING, "starting", now, now,
                    str(log_dir) if log_dir else None, os.getpid(), parent_run_id,
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
        self.set_finished_runs_since_last_review(self.finished_runs_since_last_review() + 1)

    # -- scheduler meta (alloy-4ef.19) --------------------------------------

    def _meta_get(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM scheduler_meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def _meta_set(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO scheduler_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def finished_runs_since_last_review(self) -> int:
        """Runs finished (any terminal status) since the last applied memory review."""
        try:
            return int(self._meta_get(META_FINISHED_RUNS_SINCE_REVIEW) or 0)
        except ValueError:
            return 0

    def set_finished_runs_since_last_review(self, count: int) -> None:
        self._meta_set(META_FINISHED_RUNS_SINCE_REVIEW, str(max(0, int(count))))

    def last_memory_review_day(self) -> str | None:
        """ISO date of the last scheduler-driven memory review attempt (once-per-day gate)."""
        return self._meta_get(META_LAST_MEMORY_REVIEW_DAY)

    def set_last_memory_review_day(self, day: str) -> None:
        self._meta_set(META_LAST_MEMORY_REVIEW_DAY, day)

    def mark_paused(self, run_id: str) -> None:
        """The run is waiting for a human; the clock stops here (see `elapsed`)."""
        self.update_run(run_id, paused_at=utcnow().isoformat())

    def mark_resumed(self, run_id: str) -> None:
        """Bank the time spent paused so wall-time limits ignore it."""
        record = self.get_run(run_id)
        if not record or not record.get("paused_at"):
            return
        try:
            paused_at = datetime.fromisoformat(record["paused_at"])
        except ValueError:
            self.update_run(run_id, paused_at=None)
            return
        paused_for = max(0.0, (utcnow() - paused_at).total_seconds())
        self.update_run(
            run_id, paused_at=None, paused_s=float(record.get("paused_s") or 0) + paused_for
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

    def active_runs(self, repo: Path | None = None) -> list[dict[str, Any]]:
        repo_filter = " AND repo = ?" if repo is not None else ""
        params = (RUN_DONE, RUN_FAILED, RUN_CANCELLED)
        if repo is not None:
            params += (str(repo),)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status NOT IN (?,?,?)" + repo_filter + " ORDER BY started_at",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def all_runs(self, limit: int = 100, repo: Path | None = None) -> list[dict[str, Any]]:
        repo_filter = " WHERE repo = ?" if repo is not None else ""
        params = (str(repo), limit) if repo is not None else (limit,)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs" + repo_filter + " ORDER BY started_at DESC LIMIT ?", params
            ).fetchall()
        return [dict(row) for row in rows]

    def run_status_totals(self, repo: Path) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM runs WHERE repo = ? GROUP BY status",
                (str(repo),),
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def children_of(self, run_id: str) -> list[dict[str, Any]]:
        """Remediation runs started from `run_id` (see Engine.run_child)."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE parent_run_id = ? ORDER BY started_at", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def orphaned_runs(self) -> list[dict[str, Any]]:
        """Runs marked running whose process is gone -- i.e. crash survivors.

        A child run whose parent is still running is not an orphan: the parent
        drives it and recovers it, so it is only offered for adoption once the
        parent itself is orphaned.
        """
        active = self.active_runs()
        running = {run["run_id"]: run for run in active if run["status"] == RUN_RUNNING}
        orphans = []
        for run in active:
            if run["status"] != RUN_RUNNING or _pid_alive(run["pid"]):
                continue
            parent = running.get(run.get("parent_run_id"))
            if parent is not None and _pid_alive(parent["pid"]):
                continue
            orphans.append(run)
        return orphans

    # -- agent call ledger ------------------------------------------------

    def start_call(
        self, call_id: str, *, run_id: str, bead_id: str, role: str,
        runner: str, model: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO inflight_calls (call_id, run_id, bead_id, role, runner, model, started_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (call_id, run_id, bead_id, role, runner, model, utcnow().isoformat()),
            )

    def set_call_pid(self, call_id: str, pid: int) -> None:
        """Record the harness pid once it is spawned (journal 9): the row is
        inserted before the process exists, and this is what lets
        `reconcile_inflight` and `alloy cancel` reach the process group after
        the `alloy run` that started it has died."""
        with self.connect() as conn:
            conn.execute("UPDATE inflight_calls SET pid = ? WHERE call_id = ?", (pid, call_id))

    def discard_call(self, call_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM inflight_calls WHERE call_id = ?", (call_id,))

    def active_calls(self, run_id: str | None = None) -> list[dict[str, Any]]:
        run_filter = " WHERE run_id = ?" if run_id is not None else ""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM inflight_calls" + run_filter + " ORDER BY started_at, call_id",
                (run_id,) if run_id is not None else (),
            ).fetchall()
        return [dict(row) for row in rows]

    def reconcile_inflight(self) -> list[dict[str, Any]]:
        """Remove calls left behind by processes that no longer own a live run.

        journal 9: a dead `alloy run` leaves its harness process group
        running with nobody to stop it, so the recorded harness pid is killed
        before the row goes. A live run's harness is never touched -- only
        the owner's death makes the pid safe to signal.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT inflight_calls.*, runs.pid AS run_pid FROM inflight_calls"
                " JOIN runs ON runs.run_id = inflight_calls.run_id"
            ).fetchall()
            removed = []
            for row in rows:
                if not _pid_alive(row["run_pid"]):
                    call = dict(row)
                    del call["run_pid"]
                    if call.get("pid"):
                        _terminate_group(int(call["pid"]))
                    conn.execute("DELETE FROM inflight_calls WHERE call_id = ?", (call["call_id"],))
                    removed.append(call)
        return removed

    def finish_call(
        self, call_id: str, *, run_id: str, bead_id: str, role: str,
        iteration: int, result: AgentResult,
    ) -> None:
        record = AgentCallRecord(
            run_id=run_id,
            bead_id=bead_id,
            role=role,
            runner=result.runner,
            model=result.model,
            prompt_hash=result.prompt_hash,
            # A prompt without layers (a probe, a hand-built result) has no
            # stable prefix apart from itself: its whole-prompt hash stands in.
            prefix_hash=result.prefix_hash or result.prompt_hash,
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
            conn.execute("DELETE FROM inflight_calls WHERE call_id = ?", (call_id,))
            conn.execute(
                "INSERT INTO agent_calls (run_id, bead_id, role, runner, model, prompt_hash,"
                " prefix_hash, started_at, ended_at, duration_s, exit_code, ok, usage_json,"
                " log_path, iteration, structured_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.run_id, record.bead_id, record.role, record.runner, record.model,
                    record.prompt_hash, record.prefix_hash,
                    _iso(record.started_at), _iso(record.ended_at),
                    record.duration_s, record.exit_code, int(record.ok),
                    record.usage_json, record.log_path, record.iteration,
                    json.dumps(result.structured) if result.structured is not None else None,
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

    def call_count(self, run_id: str, *, include_children: bool = False) -> int:
        """Agent calls made by the run; with `include_children`, also those of
        its remediation children, so a child spends the parent's budget."""
        query = "SELECT COUNT(*) AS n FROM agent_calls WHERE run_id = ?"
        params: tuple[Any, ...] = (run_id,)
        if include_children:
            query += " OR run_id IN (SELECT run_id FROM runs WHERE parent_run_id = ?)"
            params += (run_id,)
        with self.connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["n"]) if row else 0

    def token_totals(self, run_id: str) -> dict:
        """Token sums for the run; `cost_usd` is None unless some call reported one."""
        return _sum_usage(
            normalize(json.loads(call["usage_json"])) for call in self.agent_calls(run_id)
        )

    def models_used(self, run_id: str) -> list[dict[str, Any]]:
        """One entry per distinct (runner, model) across finished and inflight calls."""
        groups: dict[tuple[str, str | None], dict[str, Any]] = {}
        with self.connect() as conn:
            finished = conn.execute(
                "SELECT runner, model, started_at, usage_json FROM agent_calls WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            inflight = conn.execute(
                "SELECT runner, model, started_at FROM inflight_calls WHERE run_id = ?",
                (run_id,),
            ).fetchall()

        for row in finished:
            key = (row["runner"], row["model"])
            group = groups.setdefault(
                key, {"first_at": row["started_at"], "usages": [], "calls": 0}
            )
            if row["started_at"] < group["first_at"]:
                group["first_at"] = row["started_at"]
            group["calls"] += 1
            group["usages"].append(normalize(json.loads(row["usage_json"])))

        for row in inflight:
            key = (row["runner"], row["model"])
            group = groups.setdefault(
                key, {"first_at": row["started_at"], "usages": [], "calls": 0}
            )
            if row["started_at"] < group["first_at"]:
                group["first_at"] = row["started_at"]

        entries: list[dict[str, Any]] = []
        for (runner, model), group in sorted(groups.items(), key=lambda item: item[1]["first_at"]):
            entry = {
                "runner": runner,
                "model": model,
                "harness": harness_for_runner(runner),
                "calls": group["calls"],
                **_sum_usage(group["usages"]),
            }
            entries.append(entry)
        return entries

    def finished_runs_since(self, since: str, repo: Path | str) -> list[dict[str, Any]]:
        """Terminal runs for `repo` whose `ended_at` is at or after `since`, ascending."""
        statuses = tuple(TERMINAL_RUN_STATUSES)
        placeholders = ",".join("?" * len(statuses))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM runs WHERE status IN ({placeholders})"
                " AND ended_at >= ? AND repo = ? ORDER BY ended_at",
                (*statuses, since, str(repo)),
            ).fetchall()
        return [dict(row) for row in rows]

    def token_totals_by_role(self, run_id: str) -> dict[str, dict]:
        by_role: dict[str, list[dict]] = {}
        for call in self.agent_calls(run_id):
            by_role.setdefault(call["role"], []).append(normalize(json.loads(call["usage_json"])))
        return {role: _sum_usage(records) for role, records in by_role.items()}


def _sum_usage(records: Iterable[dict]) -> dict:
    """Unreported token counts sum as 0; an unreported cost stays None, since
    `0.0` would claim a free run rather than an unpriced one."""
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": None}
    for record in records:
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            totals[key] += record[key] or 0
        if record["cost_usd"] is not None:
            totals["cost_usd"] = (totals["cost_usd"] or 0.0) + record["cost_usd"]
    return totals


def _iso(value: datetime | str) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _ensure_columns(conn: sqlite3.Connection, migrations: dict[str, dict[str, str]]) -> None:
    for table, columns in migrations.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, declaration in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _pid_alive(pid: int | None) -> bool:
    from alloy.procs import pid_alive

    return pid_alive(pid)


def _terminate_group(pid: int) -> bool:
    from alloy.procs import terminate_group

    return terminate_group(pid)
