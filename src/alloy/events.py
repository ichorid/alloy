"""The attention feed: one JSON line per event a supervisor should hear about.

Alloy appends to `<root>/events.jsonl` when a run needs a human, fails,
stalls, finishes or resumes. A supervising agent tails the file with
`alloy events --follow` instead of polling `alloy status`.

Appends are single `O_APPEND` writes of one short line, so concurrent writers
(the scheduler, `alloy run`, `alloy resume`) never interleave.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from alloy.models import utcnow

EVENT_NEEDS_HUMAN = "needs-human"
EVENT_FAILED = "failed"
EVENT_STALLED = "stalled"
EVENT_DONE = "done"
EVENT_CANCELLED = "cancelled"
EVENT_RESUMED = "resumed"

# Events that mean "somebody has to look"; the rest are informational.
ATTENTION_EVENTS = frozenset({EVENT_NEEDS_HUMAN, EVENT_FAILED, EVENT_STALLED})

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_since(value: str, now: datetime | None = None) -> datetime:
    """`30m`, `2h`, `1d`, `90s` (ago) or an ISO timestamp."""
    text = value.strip()
    match = _DURATION.match(text)
    if match:
        return (now or utcnow()) - timedelta(seconds=float(match[1]) * _UNITS[match[2]])
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"not a duration (30m, 2h) or ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=utcnow().tzinfo)
    return parsed


@dataclass(frozen=True)
class EventLog:
    path: Path

    def emit(
        self, event: str, *, bead: str, run: str | None = None, reason: str = "", **extra: Any
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": utcnow().isoformat(),
            "event": event,
            "bead": bead,
            "run": run,
            "reason": reason,
            **extra,
        }
        line = json.dumps(record, default=str, separators=(",", ":")) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line.encode())
            finally:
                os.close(fd)
        except OSError:
            pass  # the feed is a courtesy; it must never fail a run
        return record

    def read(
        self, *, since: datetime | None = None, only: frozenset[str] | None = None
    ) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            record = _parse(line)
            if record is not None and _keep(record, since, only):
                events.append(record)
        return events

    def follow(
        self,
        *,
        since: datetime | None = None,
        only: frozenset[str] | None = None,
        interval: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        stop: Callable[[], bool] = lambda: False,
    ) -> Iterator[dict[str, Any]]:
        """Yield matching events forever. With `since`, replay history first;
        without it, start at the end of the file so only new events arrive."""
        offset = 0
        if since is None and self.path.exists():
            offset = self.path.stat().st_size
        pending = b""
        while not stop():
            try:
                size = self.path.stat().st_size
            except OSError:
                size = 0
            if size < offset:  # truncated or rotated
                offset, pending = 0, b""
            if size > offset:
                with open(self.path, "rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                offset += len(chunk)
                *lines, pending = (pending + chunk).split(b"\n")
                for raw in lines:
                    record = _parse(raw.decode("utf-8", errors="replace"))
                    if record is not None and _keep(record, since, only):
                        yield record
                continue
            sleep(interval)


def _parse(line: str) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) and "event" in record else None


def _keep(record: dict[str, Any], since: datetime | None, only: frozenset[str] | None) -> bool:
    if only is not None and record.get("event") not in only:
        return False
    if since is not None:
        try:
            stamp = datetime.fromisoformat(str(record.get("ts")))
        except ValueError:
            return False
        if stamp < since:
            return False
    return True


def format_line(record: dict[str, Any]) -> str:
    """One greppable line: `<ts> <event> <bead> run=<id> <reason>`."""
    parts = [str(record.get("ts", ""))[:19], str(record["event"]), str(record.get("bead", ""))]
    if record.get("run"):
        parts.append(f"run={record['run']}")
    if record.get("reason"):
        parts.append(" ".join(str(record["reason"]).split()))
    return " ".join(parts)
