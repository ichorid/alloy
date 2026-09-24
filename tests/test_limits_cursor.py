"""Cursor limits probe via dashboard API (alloy-w9d.4)."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from alloy.limits import HARNESS_LIMITS_KEYS, WINDOW_KEYS
from alloy.limits.cursor import DASHBOARD_USAGE_URL, LEGACY_USAGE_URL, probe

START_OF_MONTH = "2026-09-01T00:00:00.000Z"
BILLING_CYCLE_END_MS = 179_249_560_3000
BILLING_CYCLE_END_ISO = "2026-10-20T11:26:43+00:00"
USER_ID = "user_abc"
SUB = f"auth0|{USER_ID}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(*, sub: str | None = SUB) -> str:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload_obj: dict[str, str] = {}
    if sub is not None:
        payload_obj["sub"] = sub
    payload = _b64url(json.dumps(payload_obj).encode())
    return f"{header}.{payload}.unsigned"


VALID_JWT = make_jwt(sub=SUB)


def _dashboard_payload() -> dict:
    return {
        "billingCycleStart": "1789903603000",
        "billingCycleEnd": str(BILLING_CYCLE_END_MS),
        "planUsage": {
            "totalPercentUsed": 24.72,
            "includedSpend": 2000,
            "limit": 2000,
        },
        "enabled": True,
        "displayMessage": "You've hit your usage limit",
    }


def _dashboard_payload_with_api() -> dict:
    payload = _dashboard_payload()
    payload["planUsage"]["apiPercentUsed"] = 67.5
    return payload


def _legacy_usage_payload() -> dict:
    return {
        "gpt-4": {"numRequests": 150, "maxRequestUsage": 500},
        "gpt-4-32k": {"numRequests": 0, "maxRequestUsage": 50},
        "gpt-3.5-turbo": {"numRequests": 20, "maxRequestUsage": None},
        "startOfMonth": START_OF_MONTH,
    }


def write_auth(home: Path, *, access_token: str = VALID_JWT) -> None:
    auth_dir = home / ".config" / "cursor"
    auth_dir.mkdir(parents=True, exist_ok=True)
    payload = {"accessToken": access_token}
    (auth_dir / "auth.json").write_text(json.dumps(payload), encoding="utf-8")


@dataclass
class RecordingFetch:
    status: int = 200
    body: str = ""
    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        self.calls.append({"url": url, "headers": dict(headers)})
        return self.status, self.body


@pytest.fixture
def cursor_home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def test_probe_success_maps_dashboard_plan_usage_to_total_window(cursor_home: Path):
    write_auth(cursor_home)
    fetch = RecordingFetch(status=200, body=json.dumps(_dashboard_payload()))

    result = probe(cursor_home, fetch)

    assert set(result) == set(HARNESS_LIMITS_KEYS)
    assert result["harness"] == "cursor"
    assert result["installed"] is True
    assert result["available"] is True
    assert result["source"] == "dashboard-api"
    assert result["error"] is None
    assert result["status"] is None
    assert result["fetched_at"] == result["as_of"]
    assert result["fetched_at"] is not None

    windows = result["windows"]
    assert len(windows) == 1
    window = windows[0]
    assert set(window) == set(WINDOW_KEYS)
    assert window["key"] == "total"
    assert window["label"] == "cycle"
    assert window["model"] is None
    assert window["used_percent"] == 24.72
    assert window["resets_at"] == BILLING_CYCLE_END_ISO


def test_probe_success_sends_dashboard_request_with_bearer_token(cursor_home: Path):
    write_auth(cursor_home)
    fetch = RecordingFetch(status=200, body=json.dumps(_dashboard_payload()))

    probe(cursor_home, fetch)

    assert len(fetch.calls) == 1
    call = fetch.calls[0]
    assert call["url"] == DASHBOARD_USAGE_URL
    headers = call["headers"]
    assert headers["Authorization"] == f"Bearer {VALID_JWT}"
    assert headers["Connect-Protocol-Version"] == "1"


def test_probe_accepts_token_without_sub_claim(cursor_home: Path):
    write_auth(cursor_home, access_token=make_jwt(sub=None))
    fetch = RecordingFetch(status=200, body=json.dumps(_dashboard_payload()))

    result = probe(cursor_home, fetch)

    assert result["available"] is True
    assert result["windows"][0]["used_percent"] == 24.72


def test_probe_falls_back_to_legacy_usage_api_when_dashboard_has_no_plan_usage(
    cursor_home: Path,
):
    write_auth(cursor_home)

    def dashboard_fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
        assert url == DASHBOARD_USAGE_URL
        return 200, json.dumps({"planUsage": None, "billingCycleEnd": "0"})

    def legacy_fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
        assert url.startswith(LEGACY_USAGE_URL)
        return 200, json.dumps(_legacy_usage_payload())

    result = probe(cursor_home, fetch=dashboard_fetch, legacy_fetch=legacy_fetch)

    assert result["available"] is True
    assert result["source"] == "usage-api"
    assert abs(result["windows"][0]["used_percent"] - 27.27) < 0.01
    assert result["windows"][0]["resets_at"].startswith("2026-10-01")


def test_probe_missing_credentials_never_calls_fetch(cursor_home: Path):
    cursor_home.mkdir()

    def fail_fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
        raise AssertionError("fetch must not be called when credentials are missing")

    result = probe(cursor_home, fail_fetch)

    assert result["harness"] == "cursor"
    assert result["installed"] is True
    assert result["available"] is False
    assert result["windows"] == []
    assert result["error"] == "no credentials"


def test_probe_http_error_keeps_cursor_installed(cursor_home: Path):
    write_auth(cursor_home)
    fetch = RecordingFetch(status=401, body="")

    result = probe(cursor_home, fetch)

    assert result["harness"] == "cursor"
    assert result["installed"] is True
    assert result["available"] is False
    assert result["windows"] == []
    assert result["error"] == "HTTP 401"


def test_probe_no_quota_in_response_returns_error(cursor_home: Path):
    write_auth(cursor_home)

    def dashboard_fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
        return 200, json.dumps({"billingCycleEnd": "0"})

    def legacy_fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
        return 200, json.dumps(
            {
                "gpt-3.5-turbo": {"numRequests": 20, "maxRequestUsage": None},
                "startOfMonth": START_OF_MONTH,
            }
        )

    result = probe(cursor_home, fetch=dashboard_fetch, legacy_fetch=legacy_fetch)

    assert result["harness"] == "cursor"
    assert result["installed"] is True
    assert result["available"] is False
    assert result["windows"] == []
    assert result["error"] == "no quota in response"
