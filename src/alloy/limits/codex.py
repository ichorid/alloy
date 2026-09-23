"""Codex usage limits from the newest local session rollout.

Component A3 of docs/plans/monitor-limits.md. Codex CLI appends every
event of a session to `~/.codex/sessions/**/rollout-*.jsonl`, including
`token_count` events that carry the rate-limit snapshot the CLI itself
displays. `probe` never touches the network: it scans rollout files
newest-first by mtime, reads only the tail of each, and takes the newest
`codex` token_count with a non-null primary window for the gauges, while
`status` comes from the newest token_count of any limit_id (that is how
`workspace_member_credits_depleted` surfaces on a `premium` snapshot).

Codex rollouts report *remaining* quota in `used_percent`; Alloy inverts to
`100 - n` so gauges match other harnesses (fraction consumed).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alloy.limits import unavailable, window

HARNESS = "codex"
SOURCE = "session-rollout"
TAIL_BYTES = 64 * 1024
_NO_SAMPLE = "no local sample"


def sessions_dir(home: Path) -> Path:
    """Where Codex CLI stores session rollouts under `home`."""
    return home / ".codex" / "sessions"


def _rollout_files(home: Path) -> list[Path]:
    """Rollout files newest-first by mtime; [] when the sessions dir is missing."""
    root = sessions_dir(home)
    if not root.is_dir():
        return []
    stamped: list[tuple[float, Path]] = []
    for path in root.rglob("rollout-*.jsonl"):
        try:
            stamped.append((path.stat().st_mtime, path))
        except OSError:
            continue
    stamped.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in stamped]


def _tail_lines(path: Path) -> list[str]:
    """The last TAIL_BYTES of `path` split into lines, dropping a partial first line."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(0, size - TAIL_BYTES)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []
    if start > 0:
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""
    return data.decode("utf-8", errors="replace").splitlines()


def _token_counts(path: Path) -> list[tuple[Any, dict[str, Any]]]:
    """(timestamp, rate_limits) for each token_count event in `path`, in file order."""
    events: list[tuple[Any, dict[str, Any]]] = []
    for line in _tail_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        rate_limits = payload.get("rate_limits")
        if not isinstance(rate_limits, dict):
            continue
        events.append((record.get("timestamp"), rate_limits))
    return events


def _iso_utc(value: Any) -> str | None:
    """Epoch seconds -> ISO-8601 UTC string ending in +00:00; None when absent."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, tz=UTC).isoformat()


def _label(key: str, minutes: Any) -> str:
    if key == "primary":
        if minutes == 300:
            return "5h"
        return f"{int(minutes) // 60}h" if isinstance(minutes, (int, float)) else key
    if minutes == 10080:
        return "weekly"
    return f"{int(minutes) // 1440}d" if isinstance(minutes, (int, float)) else key


def _used_percent_from_rollout(remaining_percent: float) -> float:
    """Rollout `used_percent` is remaining quota; monitor shows consumed."""
    return 100.0 - float(remaining_percent)


def _windows(rate_limits: dict[str, Any]) -> list[dict[str, Any]]:
    """primary then secondary, skipping entries that are null or lack used_percent."""
    windows: list[dict[str, Any]] = []
    for key in ("primary", "secondary"):
        entry = rate_limits.get(key)
        if not isinstance(entry, dict):
            continue
        remaining = entry.get("used_percent")
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            continue
        label = _label(key, entry.get("window_minutes"))
        windows.append(
            window(
                key,
                label,
                _used_percent_from_rollout(remaining),
                _iso_utc(entry.get("resets_at")),
            ),
        )
    return windows


def probe(home: Path) -> dict[str, Any]:
    """Current Codex usage windows from local rollouts, or 'no local sample'."""
    codex_event: tuple[Any, dict[str, Any]] | None = None
    status_event: tuple[Any, dict[str, Any]] | None = None
    for path in _rollout_files(home):
        events = _token_counts(path)
        if not events:
            continue
        if status_event is None:
            status_event = events[-1]
        if codex_event is None:
            for event in reversed(events):
                rate_limits = event[1]
                if rate_limits.get("limit_id") == "codex" and rate_limits.get("primary") is not None:
                    codex_event = event
                    break
        if codex_event is not None and status_event is not None:
            break
    if codex_event is None:
        return unavailable(HARNESS, _NO_SAMPLE)
    timestamp, rate_limits = codex_event
    status = status_event[1].get("rate_limit_reached_type") if status_event else None
    return {
        "harness": HARNESS,
        "installed": True,
        "available": True,
        "fetched_at": datetime.now(UTC).isoformat(),
        "as_of": timestamp if isinstance(timestamp, str) else None,
        "source": SOURCE,
        "error": None,
        "status": status if isinstance(status, str) else None,
        "windows": _windows(rate_limits),
    }


__all__ = ["HARNESS", "SOURCE", "TAIL_BYTES", "probe", "sessions_dir"]
