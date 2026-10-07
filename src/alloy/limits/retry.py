"""Reading "come back later" out of harness error messages.

A Claude session limit says when it resets ("resets 1:20am"), an API error
may say "retry after 90 seconds", and a bare "rate limit" says nothing useful.
`parse_retry_at` turns each into a wall-clock time so the scheduler can resume
the run itself instead of waiting for a human to notice (journal 38).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_RATE_LIMIT_WAIT = timedelta(minutes=30)
"""What to assume when the harness says "rate limit" without a time."""

DEFAULT_BREAKER_WAIT = timedelta(hours=3)
"""How long the runner circuit breaker keeps a limited harness out of use
when its message names no reset time (see `alloy.store.Store.mark_runner_unavailable`)."""

# "resets 1:20am", and codex's "try again at 2:31 AM".
_RESETS_AT = re.compile(
    r"(?:resets?|try\s+again)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
    re.IGNORECASE,
)
_RETRY_AFTER = re.compile(
    r"(?:retry|try\s+again)\s+(?:after|in)\s+(\d+(?:\.\d+)?)\s*"
    r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b",
    re.IGNORECASE,
)
# cursor: "Your usage limits will reset when your monthly cycle ends on 10/26/2026."
_RESETS_ON_DATE = re.compile(
    r"(?:resets?|ends?)\b[^.\n]{0,60}?\bon\s+(\d{1,2})/(\d{1,2})/(\d{4})",
    re.IGNORECASE,
)
_RATE_LIMIT = re.compile(r"rate[\s_-]*limit", re.IGNORECASE)
# Harness wording for a spend/usage/session limit that is not a "rate limit"
# (journal: overnight 2026-10-06, 134 calls burned on these):
#   codex  "You've hit your usage limit. ... try again at 2:31 AM."
#   cursor "ActionRequiredError: You've hit your usage limit ... Switch to a
#          different model or set a Spend Limit to continue with this model."
#   claude "You've hit your session limit · resets 1:20am"
_USAGE_LIMIT = re.compile(
    r"hit\s+your\s+(?:\w+\s+){0,2}limit"
    r"|usage[\s_-]*limits?\s+(?:reached|exceeded|hit)"
    r"|(?:session|weekly|5-hour|daily|monthly)\s+limit\s+reached"
    r"|out\s+of\s+credits"
    r"|insufficient[\s_-]*quota",
    re.IGNORECASE,
)
# The limit is on this model, not the whole harness (cursor per-model pools).
_MODEL_SCOPED = re.compile(r"(?:switch\s+to\s+a\s+different\s+model|with\s+this\s+model)", re.IGNORECASE)

_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
}


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
    reset_on = _reset_date(error, now)
    if reset_on is not None:
        return reset_on
    if _RATE_LIMIT.search(error) or _USAGE_LIMIT.search(error):
        return now + DEFAULT_RATE_LIMIT_WAIT
    return None


def _reset_date(error: str, now: datetime) -> datetime | None:
    """Midnight (local, `now`'s tzinfo) of an "ends on MM/DD/YYYY" date."""
    match = _RESETS_ON_DATE.search(error)
    if not match:
        return None
    try:
        candidate = datetime(int(match.group(3)), int(match.group(1)), int(match.group(2)), tzinfo=now.tzinfo)
    except ValueError:
        return None
    return candidate if candidate > now else None


@dataclass(frozen=True)
class LimitHit:
    """A harness said it hit a usage, spend, session or rate limit.

    `reset_at` is the time the message itself names, or None when it names
    none (the circuit breaker then waits `DEFAULT_BREAKER_WAIT`).
    `model_scoped` means the limit is on the model, not the whole harness:
    cursor says "switch to a different model ... to continue with this model"."""

    reason: str
    reset_at: datetime | None
    model_scoped: bool = False
    strong: bool = True
    """Harness limit wording ("hit your usage limit"), not just "rate limit"."""

    def until(self, now: datetime) -> datetime:
        return self.reset_at if self.reset_at is not None else now + DEFAULT_BREAKER_WAIT


def parse_limit(error: str, now: datetime) -> LimitHit | None:
    """The limit a failed harness call reported, or None when it reported none.

    Only harness limit wording counts -- a generic error with a "retry after"
    hint is not a limit -- so a false hit cannot take a working harness out of
    rotation for hours."""
    if not error:
        return None
    if not (_USAGE_LIMIT.search(error) or _RATE_LIMIT.search(error)):
        return None
    reset_at: datetime | None = None
    match = _RESETS_AT.search(error) or _RETRY_AFTER.search(error)
    if match:
        reset_at = parse_retry_at(error, now)
    else:
        reset_at = _reset_date(error, now)
    line = next(
        (part.strip() for part in error.splitlines() if _USAGE_LIMIT.search(part) or _RATE_LIMIT.search(part)), ""
    )
    return LimitHit(
        reason=(line or error.strip())[:300],
        reset_at=reset_at,
        model_scoped=bool(_MODEL_SCOPED.search(error)),
        strong=bool(_USAGE_LIMIT.search(error)),
    )
