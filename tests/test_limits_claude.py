"""Claude Code limits probe via OAuth usage API (alloy-w9d.2)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from alloy.limits import HARNESS_LIMITS_KEYS, WINDOW_KEYS
from alloy.limits.claude import probe

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
R1 = "2026-09-23T05:00:00+00:00"
R2 = "2026-09-24T00:00:00+00:00"
VALID_TOKEN = "claude-oauth-token-abc123"
FUTURE_EXPIRY = "2099-12-31T23:59:59+00:00"
PAST_EXPIRY = "2020-01-01T00:00:00+00:00"


def _usage_payload() -> dict:
    return {
        "five_hour": {"utilization": 42, "resets_at": R1},
        "seven_day": {"utilization": 61, "resets_at": R2},
        "seven_day_opus": {"utilization": 80, "resets_at": R2},
        "seven_day_fable": {"utilization": 12, "resets_at": R2},
        "seven_day_sonnet": None,
    }


def write_credentials(
    home: Path,
    *,
    token: str = VALID_TOKEN,
    expires_at: str = FUTURE_EXPIRY,
) -> None:
    creds_dir = home / ".claude"
    creds_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "claudeAiOauth": {
            "accessToken": token,
            "expiresAt": expires_at,
        }
    }
    (creds_dir / ".credentials.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


@dataclass
class RecordingFetch:
    status: int = 200
    body: str = ""
    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        self.calls.append({"url": url, "headers": dict(headers)})
        return self.status, self.body


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def test_probe_success_maps_oauth_usage_to_four_windows(claude_home: Path):
    write_credentials(claude_home)
    fetch = RecordingFetch(status=200, body=json.dumps(_usage_payload()))

    result = probe(claude_home, fetch)

    assert set(result) == set(HARNESS_LIMITS_KEYS)
    assert result["harness"] == "claude"
    assert result["installed"] is True
    assert result["available"] is True
    assert result["source"] == "oauth-usage-api"
    assert result["error"] is None
    assert result["status"] is None
    assert result["fetched_at"] == result["as_of"]
    assert result["fetched_at"] is not None

    windows = result["windows"]
    assert len(windows) == 4
    for window in windows:
        assert set(window) == set(WINDOW_KEYS)

    assert windows[0] == {
        "key": "five_hour",
        "label": "5h",
        "used_percent": 42.0,
        "resets_at": R1,
        "model": None,
    }
    assert windows[1] == {
        "key": "seven_day",
        "label": "weekly",
        "used_percent": 61.0,
        "resets_at": R2,
        "model": None,
    }
    assert windows[2] == {
        "key": "seven_day_opus",
        "label": "weekly opus",
        "used_percent": 80.0,
        "resets_at": R2,
        "model": "opus",
    }
    assert windows[3] == {
        "key": "seven_day_fable",
        "label": "weekly fable",
        "used_percent": 12.0,
        "resets_at": R2,
        "model": "fable",
    }


def test_probe_success_sends_oauth_usage_request(claude_home: Path):
    write_credentials(claude_home)
    fetch = RecordingFetch(status=200, body=json.dumps(_usage_payload()))

    probe(claude_home, fetch)

    assert len(fetch.calls) == 1
    call = fetch.calls[0]
    assert call["url"] == USAGE_URL
    headers = call["headers"]
    assert headers["Authorization"] == f"Bearer {VALID_TOKEN}"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert headers["Accept"] == "application/json"


def test_probe_missing_credentials_never_calls_fetch(claude_home: Path):
    claude_home.mkdir()

    def fail_fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
        raise AssertionError("fetch must not be called when credentials are missing")

    result = probe(claude_home, fail_fetch)

    assert result["available"] is False
    assert result["windows"] == []
    assert result["error"] == "no credentials"


def test_probe_expired_token_never_calls_fetch(claude_home: Path):
    write_credentials(claude_home, expires_at=PAST_EXPIRY)

    def fail_fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
        raise AssertionError("fetch must not be called when token is expired")

    result = probe(claude_home, fail_fetch)

    assert result["available"] is False
    assert result["windows"] == []
    assert result["error"] == "token expired"


def test_probe_http_error_keeps_claude_installed(claude_home: Path):
    write_credentials(claude_home)
    fetch = RecordingFetch(status=429, body="")

    result = probe(claude_home, fetch)

    assert result["harness"] == "claude"
    assert result["installed"] is True
    assert result["available"] is False
    assert result["windows"] == []
    assert result["error"] == "HTTP 429"


def test_probe_bad_response_keeps_claude_installed(claude_home: Path):
    write_credentials(claude_home)
    fetch = RecordingFetch(status=200, body="nope")

    result = probe(claude_home, fetch)

    assert result["harness"] == "claude"
    assert result["installed"] is True
    assert result["available"] is False
    assert result["windows"] == []
    assert result["error"] == "bad response"
