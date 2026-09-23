"""Cursor limits probe via usage API (alloy-w9d.4)."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from alloy.limits import HARNESS_LIMITS_KEYS, WINDOW_KEYS
from alloy.limits.cursor import probe

USAGE_URL = "https://cursor.com/api/usage?user=user_abc"
START_OF_MONTH = "2026-09-01T00:00:00.000Z"
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


def _usage_payload() -> dict:
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


def test_probe_success_aggregates_model_usage_into_total_window(cursor_home: Path):
    write_auth(cursor_home)
    fetch = RecordingFetch(status=200, body=json.dumps(_usage_payload()))

    result = probe(cursor_home, fetch)

    assert set(result) == set(HARNESS_LIMITS_KEYS)
    assert result["harness"] == "cursor"
    assert result["installed"] is True
    assert result["available"] is True
    assert result["source"] == "usage-api"
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
    assert abs(window["used_percent"] - 27.27) < 0.01
    assert window["resets_at"].startswith("2026-10-01")


def test_probe_success_sends_usage_api_request_with_session_cookie(cursor_home: Path):
    write_auth(cursor_home)
    fetch = RecordingFetch(status=200, body=json.dumps(_usage_payload()))

    probe(cursor_home, fetch)

    assert len(fetch.calls) == 1
    call = fetch.calls[0]
    assert call["url"] == USAGE_URL
    headers = call["headers"]
    expected_cookie = f"WorkosCursorSessionToken={USER_ID}%3A%3A{VALID_JWT}"
    assert headers["Cookie"] == expected_cookie


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


def test_probe_bad_token_without_sub_never_calls_fetch(cursor_home: Path):
    auth_dir = cursor_home / ".config" / "cursor"
    auth_dir.mkdir(parents=True, exist_ok=True)
    payload = {"accessToken": make_jwt(sub=None)}
    (auth_dir / "auth.json").write_text(json.dumps(payload), encoding="utf-8")

    def fail_fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
        raise AssertionError("fetch must not be called when token has no sub claim")

    result = probe(cursor_home, fail_fetch)

    assert result["available"] is False
    assert result["windows"] == []
    assert result["error"] == "bad token"


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
    body = json.dumps(
        {
            "gpt-3.5-turbo": {"numRequests": 20, "maxRequestUsage": None},
            "startOfMonth": START_OF_MONTH,
        }
    )
    fetch = RecordingFetch(status=200, body=body)

    result = probe(cursor_home, fetch)

    assert result["harness"] == "cursor"
    assert result["installed"] is True
    assert result["available"] is False
    assert result["windows"] == []
    assert result["error"] == "no quota in response"
