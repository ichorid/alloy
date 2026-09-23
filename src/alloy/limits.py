"""Reading "come back later" out of harness error messages.

A Claude session limit says when it resets ("resets 1:20am"), an API error
may say "retry after 90 seconds", and a bare "rate limit" says nothing useful.
`parse_retry_at` turns each into a wall-clock time so the scheduler can resume
the run itself instead of waiting for a human to notice (journal 38).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

DEFAULT_RATE_LIMIT_WAIT = timedelta(minutes=30)
"""What to assume when the harness says "rate limit" without a time."""

_RESETS_AT = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE)
_RETRY_AFTER = re.compile(
    r"retry\s+(?:after|in)\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b",
    re.IGNORECASE,
)
_RATE_LIMIT = re.compile(r"rate[\s_-]*limit", re.IGNORECASE)

_UNIT_SECONDS = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60,
                 "h": 3600, "hr": 3600, "hour": 3600}


def parse_retry_at(error: str, now: datetime) -> datetime | None:
    """When the harness says it will be available again, or None.

    `now` is the reference clock; the result carries the same tzinfo (naive
    local time in, naive local time out). "resets H:MM(am|pm)" is the next
    such local time after `now`.
    """
    if not error:
        return None
    match = _RESETS_AT.search(error)
    if match:
        hour = int(match.group(1)) % 12
        if match.group(3).lower() == "pm":
            hour += 12
        minute = int(match.group(2) or 0)
        if hour > 23 or minute > 59:
            return None
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    match = _RETRY_AFTER.search(error)
    if match:
        unit = match.group(2).lower().rstrip("s") or "s"
        return now + timedelta(seconds=float(match.group(1)) * _UNIT_SECONDS[unit])
    if _RATE_LIMIT.search(error):
        return now + DEFAULT_RATE_LIMIT_WAIT
    return None
