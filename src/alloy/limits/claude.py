"""Claude Code usage limits from the OAuth usage endpoint.

Component A2 of docs/plans/monitor-limits.md. Claude Code keeps its OAuth
token in `~/.claude/.credentials.json`; the same token authorises the usage
API that the CLI's own `/usage` view reads. Every top-level object carrying
a `utilization` field becomes one Window, so per-model weekly gauges
(`seven_day_opus`, `seven_day_fable`, ...) appear without a hard-coded model
list. `probe` is pure over the injected `home` and `fetch`; only
`default_fetch` touches the network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alloy.limits import unavailable, window

HARNESS = "claude"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
SOURCE = "oauth-usage-api"
_BETA_HEADER = "oauth-2025-04-20"
_TIMEOUT_SECONDS = 10.0

Fetch = Callable[[str, dict[str, str]], tuple[int, str]]
"""GET `url` with `headers`; returns (HTTP status, body text)."""


def credentials_path(home: Path) -> Path:
    """Where Claude Code stores its OAuth credentials under `home`."""
    return home / ".claude" / ".credentials.json"


def default_fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
    """urllib GET with a short timeout. Status 0 means no HTTP response at all."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return 0, ""


def _parse_expiry(value: Any) -> datetime | None:
    """`expiresAt` as an aware UTC datetime; None when absent or unparseable.

    Claude Code writes epoch milliseconds; ISO-8601 strings are accepted too.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return _parse_expiry(int(text))
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None


def _read_token(home: Path, now: datetime) -> tuple[str | None, str | None]:
    """(access token, None) or (None, error string)."""
    try:
        data = json.loads(credentials_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "no credentials"
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        return None, "no credentials"
    expiry = _parse_expiry(oauth.get("expiresAt"))
    if expiry is not None and expiry <= now:
        return None, "token expired"
    return token, None


def _include_window_key(key: str) -> bool:
    """Only surface account and per-model weekly gauges, not stray API keys."""
    return key in ("five_hour", "seven_day") or key.startswith("seven_day_")


def _label(key: str) -> tuple[str, str | None]:
    """(label, model) for a usage key."""
    if key == "five_hour":
        return "5h", None
    if key == "seven_day":
        return "weekly", None
    if key.startswith("seven_day_"):
        model = key[len("seven_day_"):]
        return f"weekly {model}", model
    return key, None


def _windows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Windows in display order: 5h, weekly, then the rest as the API listed them."""
    ordered = [k for k in ("five_hour", "seven_day") if k in payload]
    ordered += [k for k in payload if k not in ("five_hour", "seven_day")]
    windows: list[dict[str, Any]] = []
    for key in ordered:
        if not _include_window_key(key):
            continue
        entry = payload[key]
        if not isinstance(entry, dict) or "utilization" not in entry:
            continue
        utilization = entry["utilization"]
        if isinstance(utilization, bool) or not isinstance(utilization, (int, float)):
            continue
        resets_at = entry.get("resets_at")
        label, model = _label(key)
        windows.append(
            window(key, label, utilization, resets_at if isinstance(resets_at, str) else None, model)
        )
    return windows


def probe(home: Path, fetch: Fetch = default_fetch) -> dict[str, Any]:
    """Current Claude Code usage windows, or why they could not be read.

    Never calls `fetch` without a live token. Errors: 'no credentials',
    'token expired', 'HTTP <status>', 'bad response'.
    """
    now = datetime.now(UTC)
    token, error = _read_token(home, now)
    if token is None:
        return unavailable(HARNESS, error or "no credentials")
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": _BETA_HEADER,
        "Accept": "application/json",
    }
    status, body = fetch(USAGE_URL, headers)
    if status != 200:
        return unavailable(HARNESS, f"HTTP {status}")
    try:
        payload = json.loads(body)
    except ValueError:
        return unavailable(HARNESS, "bad response")
    if not isinstance(payload, dict):
        return unavailable(HARNESS, "bad response")
    as_of = now.isoformat()
    return {
        "harness": HARNESS,
        "installed": True,
        "available": True,
        "fetched_at": as_of,
        "as_of": as_of,
        "source": SOURCE,
        "error": None,
        "status": None,
        "windows": _windows(payload),
    }


__all__ = ["SOURCE", "USAGE_URL", "credentials_path", "default_fetch", "probe"]
