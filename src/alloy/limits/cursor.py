"""Cursor usage limits from the Cursor dashboard API.

Component A4 of docs/plans/monitor-limits.md. Cursor keeps its session JWT
in `~/.config/cursor/auth.json`; the same token powers
``cursor-agent status`` and the dashboard Connect endpoint behind the CLI
``/usage`` view. ``probe`` POSTs to
``api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage`` and
maps ``planUsage.totalPercentUsed`` to one ``total`` cycle window.
``billingCycleEnd`` (epoch ms) becomes ``resets_at``. When the dashboard
response has no plan usage, the legacy ``cursor.com/api/usage`` cookie
endpoint is tried for older personal-plan shapes.
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
DASHBOARD_USAGE_URL = (
    "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
)
LEGACY_USAGE_URL = "https://cursor.com/api/usage"
SOURCE = "dashboard-api"
_LEGACY_SOURCE = "usage-api"
_COOKIE_NAME = "WorkosCursorSessionToken"
_TIMEOUT_SECONDS = 10.0

Fetch = Callable[[str, dict[str, str]], tuple[int, str]]
"""Call the usage endpoint; returns (HTTP status, body text)."""


def credentials_path(home: Path) -> Path:
    """Where Cursor stores its session token under `home`."""
    return home / ".config" / "cursor" / "auth.json"


def default_fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
    """POST with a short timeout. Status 0 means no HTTP response at all."""
    request = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return 0, ""


def default_legacy_fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
    """GET for the legacy cursor.com usage endpoint."""
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
    """`<user_id>` from the JWT `sub` claim (`<provider>|<user_id>`); unverified."""
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


def _epoch_ms_to_iso(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        ms = float(value)
    except ValueError:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


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


def _dashboard_window(payload: dict[str, Any]) -> dict[str, Any] | None:
    """One `total` cycle window from GetCurrentPeriodUsage, or None."""
    plan = payload.get("planUsage")
    if not isinstance(plan, dict):
        return None
    used = plan.get("totalPercentUsed")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    return window(
        "total",
        "cycle",
        float(used),
        _epoch_ms_to_iso(payload.get("billingCycleEnd")),
        None,
    )


def _legacy_total_window(payload: dict[str, Any]) -> dict[str, Any] | None:
    """One `total` cycle window from the legacy per-model usage dict, or None."""
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


def _available_sample(
    *,
    windows: list[dict[str, Any]],
    source: str,
    status: str | None = None,
) -> dict[str, Any]:
    as_of = datetime.now(UTC).isoformat()
    return {
        "harness": HARNESS,
        "installed": True,
        "available": True,
        "fetched_at": as_of,
        "as_of": as_of,
        "source": source,
        "error": None,
        "status": status,
        "windows": windows,
    }


def _fetch_dashboard(token: str, fetch: Fetch) -> tuple[int, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "Accept": "application/json",
    }
    return fetch(DASHBOARD_USAGE_URL, headers)


def _fetch_legacy(token: str, legacy_fetch: Fetch) -> tuple[int, str]:
    user_id = _jwt_user_id(token)
    if user_id is None:
        return 0, ""
    url = f"{LEGACY_USAGE_URL}?{urllib.parse.urlencode({'user': user_id})}"
    headers = {
        "Cookie": f"{_COOKIE_NAME}={user_id}%3A%3A{token}",
        "Accept": "application/json",
    }
    return legacy_fetch(url, headers)


def probe(
    home: Path,
    fetch: Fetch = default_fetch,
    legacy_fetch: Fetch = default_legacy_fetch,
) -> dict[str, Any]:
    """Current Cursor cycle usage as one window, or why it could not be read.

    Errors: 'no credentials', 'HTTP <status>', 'bad response',
    'no quota in response'.
    """
    token = _read_token(home)
    if token is None:
        return unavailable(HARNESS, "no credentials")

    status, body = _fetch_dashboard(token, fetch)
    if status == 200:
        try:
            payload = json.loads(body)
        except ValueError:
            return unavailable(HARNESS, "bad response")
        if isinstance(payload, dict):
            total = _dashboard_window(payload)
            if total is not None:
                return _available_sample(windows=[total], source=SOURCE)

    legacy_status, legacy_body = _fetch_legacy(token, legacy_fetch)
    if legacy_status == 200:
        try:
            legacy_payload = json.loads(legacy_body)
        except ValueError:
            return unavailable(HARNESS, "bad response")
        if isinstance(legacy_payload, dict):
            total = _legacy_total_window(legacy_payload)
            if total is not None:
                return _available_sample(windows=[total], source=_LEGACY_SOURCE)

    if status not in (0, 200):
        return unavailable(HARNESS, f"HTTP {status}")
    return unavailable(HARNESS, "no quota in response")


__all__ = [
    "DASHBOARD_USAGE_URL",
    "LEGACY_USAGE_URL",
    "SOURCE",
    "credentials_path",
    "default_fetch",
    "default_legacy_fetch",
    "probe",
]
