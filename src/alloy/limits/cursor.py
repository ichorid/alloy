"""Cursor usage limits from the cursor.com usage API.

Component A4 of docs/plans/monitor-limits.md. Cursor keeps its session JWT
in `~/.config/cursor/auth.json`; the `sub` claim (`<provider>|<user_id>`)
names the account, and `user_id::token` in the `WorkosCursorSessionToken`
cookie authorises `https://cursor.com/api/usage`. The per-model entries are
reduced to one `total` window for the current billing cycle:
100 * sum(numRequests) / sum(maxRequestUsage) over entries with a positive
`maxRequestUsage`, resetting one month after `startOfMonth`. `probe` is pure
over the injected `home` and `fetch`; only `default_fetch` touches the network.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alloy.limits import unavailable, window

HARNESS = "cursor"
USAGE_URL = "https://cursor.com/api/usage"
SOURCE = "usage-api"
_COOKIE_NAME = "WorkosCursorSessionToken"
_TIMEOUT_SECONDS = 10.0

Fetch = Callable[[str, dict[str, str]], tuple[int, str]]
"""GET `url` with `headers`; returns (HTTP status, body text)."""


def credentials_path(home: Path) -> Path:
    """Where Cursor stores its session token under `home`."""
    return home / ".config" / "cursor" / "auth.json"


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


def _read_token(home: Path) -> str | None:
    """The `accessToken` string from auth.json, or None when missing/unreadable."""
    try:
        data = json.loads(credentials_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = data.get("accessToken") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        return None
    return token


def _jwt_user_id(token: str) -> str | None:
    """`<user_id>` from the JWT `sub` claim (`<provider>|<user_id>`); unverified.

    None when the token is not a three-segment JWT, the payload is not
    base64url JSON, or `sub` is missing.
    """
    segments = token.split(".")
    if len(segments) != 3:
        return None
    payload_segment = segments[1]
    padded = payload_segment + "=" * (-len(payload_segment) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, UnicodeDecodeError):
        return None
    sub = payload.get("sub") if isinstance(payload, dict) else None
    if not isinstance(sub, str) or not sub:
        return None
    user_id = sub.split("|", 1)[-1]
    return user_id or None


def _add_month(value: str) -> str | None:
    """ISO timestamp one calendar month after `value`; None when unparseable."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if parsed.month == 12:
        bumped = parsed.replace(year=parsed.year + 1, month=1)
    else:
        bumped = parsed.replace(month=parsed.month + 1)
    return bumped.isoformat()


def _count(value: Any) -> float | None:
    """A non-bool number, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _total_window(payload: dict[str, Any]) -> dict[str, Any] | None:
    """One `total` cycle window, or None when no entry carries a positive quota."""
    used = 0.0
    quota = 0.0
    for key, entry in payload.items():
        if key == "startOfMonth" or not isinstance(entry, dict):
            continue
        max_usage = _count(entry.get("maxRequestUsage"))
        if max_usage is None or max_usage <= 0:
            continue
        quota += max_usage
        used += _count(entry.get("numRequests")) or 0.0
    if quota <= 0:
        return None
    start = payload.get("startOfMonth")
    resets_at = _add_month(start) if isinstance(start, str) else None
    return window("total", "cycle", 100 * used / quota, resets_at, None)


def probe(home: Path, fetch: Fetch = default_fetch) -> dict[str, Any]:
    """Current Cursor cycle usage as one window, or why it could not be read.

    Never calls `fetch` without a decodable token. Errors: 'no credentials',
    'bad token', 'HTTP <status>', 'bad response', 'no quota in response'.
    """
    token = _read_token(home)
    if token is None:
        return unavailable(HARNESS, "no credentials")
    user_id = _jwt_user_id(token)
    if user_id is None:
        return unavailable(HARNESS, "bad token")
    url = f"{USAGE_URL}?{urllib.parse.urlencode({'user': user_id})}"
    headers = {
        "Cookie": f"{_COOKIE_NAME}={user_id}%3A%3A{token}",
        "Accept": "application/json",
    }
    status, body = fetch(url, headers)
    if status != 200:
        return unavailable(HARNESS, f"HTTP {status}")
    try:
        payload = json.loads(body)
    except ValueError:
        return unavailable(HARNESS, "bad response")
    if not isinstance(payload, dict):
        return unavailable(HARNESS, "bad response")
    total = _total_window(payload)
    if total is None:
        return unavailable(HARNESS, "no quota in response")
    as_of = datetime.now(UTC).isoformat()
    return {
        "harness": HARNESS,
        "installed": True,
        "available": True,
        "fetched_at": as_of,
        "as_of": as_of,
        "source": SOURCE,
        "error": None,
        "status": None,
        "windows": [total],
    }


__all__ = ["SOURCE", "USAGE_URL", "credentials_path", "default_fetch", "probe"]
