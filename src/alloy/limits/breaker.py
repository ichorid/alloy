"""When a rate-limited harness becomes available again, and noticing it early.

The runner circuit breaker (`alloy.runtime.RunContext`, table
`runner_breaker`) keeps a harness that reported a usage limit out of
rotation. Two questions decide for how long:

* **Which window blocks it.** Codex has a 5-hour (`primary`) and a weekly
  (`secondary`) window, Claude a 5-hour and weekly ones, Cursor a monthly
  cycle. The limit message names one time ("try again at 2:31 AM", "try again
  at Oct 14th, 2026 5:31 AM"); the harness's own rate-limit state (codex
  session rollouts, the `limits.json` cache) says which windows are
  exhausted and when each resets. `resolve` combines both: the harness is
  available only once every exhausted window has reset, so the latest reset
  wins.
* **Whether it came back early.** The owner can reset or buy limits by hand.
  `recovery_verdict` re-reads the state while a harness is blocked and
  clears the mark as soon as the blocking windows are no longer exhausted;
  harnesses whose state cannot be read get a probe call at a bounded
  cadence (`next_probe_at`).

Everything here is pure over its inputs except `read_state`, which only reads
local files (never a model call, never the network).
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from alloy.limits.retry import DEFAULT_BREAKER_WAIT, LimitHit

EXHAUSTED_PERCENT = 99.5
"""A window at or above this used_percent is exhausted (codex reports 100.0)."""

MATCH_TOLERANCE = timedelta(minutes=2)
"""A message time this close to a window's reset names that window (messages
have minute resolution: "2:31 AM" for a reset at 02:31:11)."""

CHECK_INTERVAL = timedelta(minutes=5)
"""How often a blocked harness's rate-limit state is re-read."""

PROBE_INTERVAL = timedelta(minutes=30)
"""How often a blocked runner without fresh readable state gets one probe call."""

PROBE_JITTER = 0.2
"""+-20% jitter on `PROBE_INTERVAL`, so probes of several harnesses spread out."""

WINDOWS = ("5h", "weekly", "month", "unknown")
SOURCES = ("window", "message", "default")

StateReader = Callable[[str], "dict[str, Any] | None"]
"""harness -> a HarnessLimits sample (see `alloy.limits.window`) or None."""


@dataclass(frozen=True)
class Availability:
    """When a limited harness may run again and why.

    `window` is the window that blocks it (5h/weekly/month/unknown), `source`
    where `until` came from: the harness's own rate-limit `window` data, the
    limit `message`, or the `default` wait. `window_keys` are the state's
    window keys (e.g. codex `primary`/`secondary`) the recovery check
    watches."""

    until: datetime
    window: str
    source: str
    window_keys: tuple[str, ...] = ()


def window_kind(entry: dict[str, Any]) -> str:
    """5h/weekly/month/unknown for one state window."""
    label = str(entry.get("label") or "").lower()
    key = str(entry.get("key") or "").lower()
    if label == "5h" or key == "five_hour":
        return "5h"
    if label.startswith("weekly") or key.startswith("seven_day") or label in ("7d", "1w"):
        return "weekly"
    if label in ("cycle", "month", "monthly") or key == "total":
        return "month"
    return "unknown"


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.astimezone()
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Window:
    key: str
    kind: str
    used: float
    resets_at: datetime | None


def _windows(sample: dict[str, Any] | None) -> list[_Window]:
    if not isinstance(sample, dict) or not sample.get("available", True):
        return []
    result = []
    for entry in sample.get("windows") or []:
        if not isinstance(entry, dict):
            continue
        used = entry.get("used_percent")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        result.append(
            _Window(str(entry.get("key") or ""), window_kind(entry), float(used), _aware(entry.get("resets_at")))
        )
    return result


def sample_time(sample: dict[str, Any] | None) -> datetime | None:
    """When a state sample was taken (`as_of`, else `fetched_at`)."""
    if not isinstance(sample, dict):
        return None
    return _aware(sample.get("as_of")) or _aware(sample.get("fetched_at"))


def resolve(
    hit: LimitHit,
    sample: dict[str, Any] | None,
    *,
    failed_at: datetime,
    now: datetime,
) -> Availability:
    """The next moment the harness is available again after `hit`.

    * A message time that matches a state window's reset (within
      `MATCH_TOLERANCE`) names that window; its exact reset is used. Other
      exhausted windows count too when the sample is at least as new as the
      failure.
    * A message time that matches no window: the window data wins when the
      sample is at least as new as the failure (`failed_at`) and shows an
      exhausted window; otherwise the message wins.
    * No message time: the latest reset among exhausted windows, else
      `DEFAULT_BREAKER_WAIT`.

    Several exhausted windows -> the latest reset (available only when every
    one is). `now` and `failed_at` are aware; `hit.reset_at` may be naive local.
    """
    message_at = _aware(hit.reset_at) if hit.reset_at is not None else None
    windows = _windows(sample)
    taken = sample_time(sample)
    fresh = taken is not None and taken >= failed_at
    exhausted = [w for w in windows if w.used >= EXHAUSTED_PERCENT and w.resets_at is not None and w.resets_at > now]

    def latest(blocking: list[_Window]) -> Availability:
        top = max(blocking, key=lambda w: w.resets_at)  # type: ignore[arg-type, return-value]
        until = max(top.resets_at, now + timedelta(minutes=1))  # type: ignore[type-var]
        return Availability(until, top.kind, "window", tuple(sorted({w.key for w in blocking})))

    if message_at is not None:
        matched = [w for w in windows if w.resets_at is not None and abs(w.resets_at - message_at) <= MATCH_TOLERANCE]
        if matched:
            blocking = matched + ([w for w in exhausted if w not in matched] if fresh else [])
            return latest(blocking)
        if fresh and exhausted:
            return latest(exhausted)
        return Availability(max(message_at, now + timedelta(minutes=1)), hit.window, "message")
    if exhausted:
        return latest(exhausted)
    return Availability(now + DEFAULT_BREAKER_WAIT, "unknown", "default")


def recovery_verdict(
    sample: dict[str, Any] | None,
    *,
    window: str,
    window_keys: tuple[str, ...],
    marked_at: datetime,
    now: datetime,
) -> str | None:
    """What fresh rate-limit state says about a breaker mark.

    "reset" -- state newer than the mark shows every blocking window below
    `EXHAUSTED_PERCENT` or past its reset; "blocked" -- newer state still
    shows a blocking window exhausted; None -- no state newer than the mark
    (or none of the blocking windows in it), so nothing is known."""
    taken = sample_time(sample)
    if taken is None or taken <= marked_at:
        return None
    windows = _windows(sample)
    if window_keys:
        watched = [w for w in windows if w.key in window_keys]
    elif window in ("5h", "weekly", "month"):
        watched = [w for w in windows if w.kind == window]
    else:
        watched = windows
    if not watched:
        return None
    for w in watched:
        if w.used >= EXHAUSTED_PERCENT and (w.resets_at is None or w.resets_at > now):
            return "blocked"
    return "reset"


def next_probe_at(now: datetime, rng: random.Random | None = None) -> datetime:
    """`now` + `PROBE_INTERVAL` with +-`PROBE_JITTER` jitter."""
    factor = 1.0 + (rng or random).uniform(-PROBE_JITTER, PROBE_JITTER)
    return now + PROBE_INTERVAL * factor


def read_state(harness: str, *, cache_path: Path | None, home: Path | None = None) -> dict[str, Any] | None:
    """The harness's current rate-limit state from local files only.

    Codex: the newest session rollout (`alloy.limits.codex.probe`). Every
    harness: the `limits.json` cache that `alloy limits` and the monitor
    refresh. The newer sample wins. None in fake-harness test mode
    (`ALLOY_FAKE_CONFIG`) and when nothing is readable."""
    if os.environ.get("ALLOY_FAKE_CONFIG"):
        return None
    samples: list[dict[str, Any]] = []
    if harness == "codex":
        try:
            from alloy.limits import codex

            sample = codex.probe(home or Path.home())
            if sample.get("available"):
                samples.append(sample)
        except Exception:
            pass
    if cache_path is not None:
        try:
            import json

            cached = json.loads(cache_path.read_text(encoding="utf-8")).get(harness)
            if isinstance(cached, dict) and cached.get("available"):
                samples.append(cached)
        except (OSError, ValueError, AttributeError):
            pass
    if not samples:
        return None
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return max(samples, key=lambda s: sample_time(s) or epoch)


__all__ = [
    "CHECK_INTERVAL",
    "EXHAUSTED_PERCENT",
    "MATCH_TOLERANCE",
    "PROBE_INTERVAL",
    "Availability",
    "StateReader",
    "next_probe_at",
    "read_state",
    "recovery_verdict",
    "resolve",
    "sample_time",
    "window_kind",
]
