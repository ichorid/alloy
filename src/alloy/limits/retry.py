"""Reading "come back later" out of harness error messages.

A Claude session limit says when it resets ("resets 1:20am"), an API error
may say "retry after 90 seconds", and a bare "rate limit" says nothing useful.
`parse_retry_at` turns each into a wall-clock time so the scheduler can resume
the run itself instead of waiting for a human to notice (journal 38).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
# codex: "... or try again at Oct 14th, 2026 5:31 AM." -- a named month, an
# optional ordinal suffix, optional year, optional time (12h or 24h) and an
# optional timezone abbreviation. Without a year it is the next such date.
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
# Fixed offsets (hours) for the abbreviations a harness is likely to print.
_TZ_OFFSETS = {
    "UTC": 0, "GMT": 0, "Z": 0,
    "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8, "PDT": -7,
    "AKST": -9, "AKDT": -8, "HST": -10,
    "WET": 0, "WEST": 1, "BST": 1, "CET": 1, "CEST": 2, "EET": 2, "EEST": 3, "MSK": 3,
    "JST": 9, "KST": 9, "AEST": 10, "AEDT": 11,
}  # fmt: skip
_RESETS_AT_DATE = re.compile(
    r"(?:resets?|try\s+again|available\s+again)\s+(?:(?:at|on)\s+)?"
    r"(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?"
    r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?\b"
    r"(?:,?\s+(?P<year>\d{4})\b(?!:))?"
    r"(?:,?\s+(?:at\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?:\s*(?P<ampm>[ap]\.?m\b\.?))?"
    r"(?:\s*\(?(?P<tz>(?:UTC|GMT)\s*[+-]\s*\d{1,2}(?::?\d{2})?|"
    + "|".join(sorted(_TZ_OFFSETS, key=len, reverse=True))
    + r")\b\)?)?)?",
    re.IGNORECASE,
)
_TZ_NUMERIC = re.compile(r"(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", re.IGNORECASE)
_JUST_PASSED = timedelta(minutes=30)
"""A bare "try again at H:MM" this little in the past means now, not tomorrow
(2026-10-09: "1:57 AM" read at 01:57:27 kept codex out for a day)."""
_MAX_RESET_AHEAD = timedelta(days=366)
"""A named reset date further out than this is nonsense, not a limit window."""
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
    such local time after `now`; "try again at Oct 14th, 2026 5:31 AM" is that
    local date and time (the next such date when the year is left out).
    A message naming no usable time falls back to `DEFAULT_RATE_LIMIT_WAIT`
    when it mentions a limit at all.
    """
    if not error:
        return None
    reset_at = _named_reset(error, now)
    if reset_at is not None:
        return reset_at
    if _RATE_LIMIT.search(error) or _USAGE_LIMIT.search(error):
        return now + DEFAULT_RATE_LIMIT_WAIT
    return None


def _named_reset(error: str, now: datetime) -> datetime | None:
    """The reset time the message itself names, or None (no defaults)."""
    return _named_reset_kind(error, now)[0]


def _named_reset_kind(error: str, now: datetime) -> tuple[datetime | None, str]:
    """(reset time the message names or None, which wording named it:
    "date", "clock", "after", "cycle" or "" when none)."""
    match = _RESETS_AT_DATE.search(error)
    if match:
        # A named date that does not parse (Feb 30, a past year) is unparsed:
        # do not let the bare "try again at H:MM" reading guess around it.
        return _month_date(match, now), "date"
    match = _RESETS_AT.search(error)
    if match:
        return _clock_reset(match, now), "clock"
    match = _RETRY_AFTER.search(error)
    if match:
        unit = match.group(2).lower().rstrip("s") or "s"
        return now + timedelta(seconds=float(match.group(1)) * _UNIT_SECONDS[unit]), "after"
    reset = _reset_date(error, now)
    return reset, "cycle" if reset is not None else ""


def _clock_reset(match: re.Match[str], now: datetime) -> datetime | None:
    """The next local occurrence of a bare "resets/try again at H:MM am|pm"."""
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == "pm":
        hour += 12
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        if now - candidate <= _JUST_PASSED:
            # The message names minutes and the window resets at some second
            # of that minute: a call in the named minute (codex at 15:40:26
            # said "3:40 PM", the window reset at 15:40:42) or just past it
            # means "any moment now", not this time tomorrow.
            return max(candidate + timedelta(minutes=1), now + timedelta(minutes=1))
        candidate += timedelta(days=1)
    return candidate


def _month_date(match: re.Match[str], now: datetime) -> datetime | None:
    """The datetime of a `_RESETS_AT_DATE` match in `now`'s tzinfo, or None
    when it is invalid, already past, or implausibly far ahead."""
    month = _MONTHS[match.group("month")[:3].lower()]
    day = int(match.group("day"))
    clock = _clock(match)
    if clock is None:
        return None
    hour, minute = clock
    tz = _tzinfo(match.group("tz"))

    def build(year: int) -> datetime | None:
        try:
            naive = datetime(year, month, day, hour, minute)
        except ValueError:
            return None
        if tz is None:
            return naive.replace(tzinfo=now.tzinfo)
        aware = naive.replace(tzinfo=tz)
        # Same convention as the rest: naive local in, naive local out.
        return aware.astimezone(now.tzinfo) if now.tzinfo else aware.astimezone().replace(tzinfo=None)

    if match.group("year"):
        candidate = build(int(match.group("year")))
    else:
        candidate = build(now.year)
        if candidate is None or candidate <= now:
            candidate = build(now.year + 1)
    if candidate is None or candidate <= now or candidate - now > _MAX_RESET_AHEAD:
        return None
    return candidate


def _clock(match: re.Match[str]) -> tuple[int, int] | None:
    """(hour, minute) of a `_RESETS_AT_DATE` match (midnight when it names no
    time), or None when the time is invalid."""
    if match.group("hour") is None:
        return 0, 0
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = match.group("ampm")
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm[0].lower() == "p" else 0)
    elif match.group("minute") is None:
        return None  # a bare number with neither ":MM" nor am/pm is no time
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _tzinfo(name: str | None) -> timezone | None:
    if not name:
        return None
    numeric = _TZ_NUMERIC.fullmatch(name.strip())
    if numeric:
        sign = -1 if numeric.group(1) == "-" else 1
        hours, minutes = int(numeric.group(2)), int(numeric.group(3) or 0)
        if hours > 14 or minutes > 59:
            return None
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    offset = _TZ_OFFSETS.get(name.upper())
    return None if offset is None else timezone(timedelta(hours=offset))


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


SHORT_WINDOW_MAX = timedelta(hours=6)
"""A named reset at most this far ahead is the short (5-hour) window; further
out it is the weekly one."""

_MONTHLY = re.compile(r"monthly|billing\s+cycle|this\s+month", re.IGNORECASE)


def message_window(reset_at: datetime | None, kind: str, error: str, now: datetime) -> str:
    """Which usage window a message's reset time belongs to, by wording and
    magnitude: "month" (cursor's monthly cycle), "5h" (within
    `SHORT_WINDOW_MAX`), "weekly" (days away) or "unknown"."""
    if reset_at is None or kind in ("", "after"):
        return "unknown"
    if kind == "cycle" or (_MONTHLY.search(error) and reset_at - now > timedelta(days=8)):
        return "month"
    return "5h" if reset_at - now <= SHORT_WINDOW_MAX else "weekly"


@dataclass(frozen=True)
class LimitHit:
    """A harness said it hit a usage, spend, session or rate limit.

    `reset_at` is the time the message itself names, or None when it names
    none (the circuit breaker then waits `DEFAULT_BREAKER_WAIT`).
    `model_scoped` means the limit is on the model, not the whole harness:
    cursor says "switch to a different model ... to continue with this model".
    `window` is the window the message's time belongs to (see
    `message_window`); the breaker cross-checks it with the harness's own
    rate-limit state (`alloy.limits.breaker.resolve`)."""

    reason: str
    reset_at: datetime | None
    model_scoped: bool = False
    strong: bool = True
    """Harness limit wording ("hit your usage limit"), not just "rate limit"."""
    window: str = "unknown"

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
    reset_at, kind = _named_reset_kind(error, now)
    line = next(
        (part.strip() for part in error.splitlines() if _USAGE_LIMIT.search(part) or _RATE_LIMIT.search(part)), ""
    )
    return LimitHit(
        reason=(line or error.strip())[:300],
        reset_at=reset_at,
        model_scoped=bool(_MODEL_SCOPED.search(error)),
        strong=bool(_USAGE_LIMIT.search(error)),
        window=message_window(reset_at, kind, error, now),
    )
