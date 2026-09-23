"""Codex limits probe via local session rollout JSONL (alloy-w9d.3)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from alloy.limits import HARNESS_LIMITS_KEYS, WINDOW_KEYS
from alloy.limits.codex import probe

CODEX_TS = "2026-09-20T14:30:00+00:00"
PREMIUM_TS = "2026-09-23T08:15:00+00:00"
RESETS_AT_EPOCH = 1789183937
RESETS_AT_ISO = "2026-09-12T03:32:17+00:00"


def _rate_limits(
    *,
    limit_id: str,
    primary: dict | None,
    secondary: dict | None = None,
    rate_limit_reached_type: str | None = None,
) -> dict:
    return {
        "limit_id": limit_id,
        "primary": primary,
        "secondary": secondary,
        "rate_limit_reached_type": rate_limit_reached_type,
    }


def _primary(used_percent: float, window_minutes: int, resets_at: int | None = None) -> dict:
    entry: dict = {"used_percent": used_percent, "window_minutes": window_minutes}
    if resets_at is not None:
        entry["resets_at"] = resets_at
    return entry


def _token_count_line(
    timestamp: str,
    *,
    limit_id: str,
    primary: dict | None,
    secondary: dict | None = None,
    rate_limit_reached_type: str | None = None,
    ordinal: int = 1,
) -> str:
    payload = {
        "type": "token_count",
        "rate_limits": _rate_limits(
            limit_id=limit_id,
            primary=primary,
            secondary=secondary,
            rate_limit_reached_type=rate_limit_reached_type,
        ),
    }
    return json.dumps(
        {
            "timestamp": timestamp,
            "ordinal": ordinal,
            "type": "event",
            "payload": payload,
        }
    )


def write_rollout(
    home: Path,
    session: str,
    filename: str,
    lines: list[str],
    *,
    mtime: float | None = None,
) -> Path:
    path = home / ".codex" / "sessions" / session / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def test_probe_cross_file_windows_from_codex_status_from_newest_token_count(codex_home: Path):
    older_mtime = 1_700_000_000.0
    newer_mtime = 1_800_000_000.0
    write_rollout(
        codex_home,
        "older-session",
        "rollout-older.jsonl",
        [
            _token_count_line(
                CODEX_TS,
                limit_id="codex",
                primary=_primary(53, 300, RESETS_AT_EPOCH),
                secondary=_primary(51, 10080, RESETS_AT_EPOCH),
            ),
        ],
        mtime=older_mtime,
    )
    write_rollout(
        codex_home,
        "newer-session",
        "rollout-newer.jsonl",
        [
            _token_count_line(
                PREMIUM_TS,
                limit_id="premium",
                primary=None,
                rate_limit_reached_type="workspace_member_credits_depleted",
            ),
        ],
        mtime=newer_mtime,
    )

    result = probe(codex_home)

    assert set(result) == set(HARNESS_LIMITS_KEYS)
    assert result["harness"] == "codex"
    assert result["installed"] is True
    assert result["available"] is True
    assert result["source"] == "session-rollout"
    assert result["error"] is None
    assert result["as_of"] == CODEX_TS
    assert result["status"] == "workspace_member_credits_depleted"

    windows = result["windows"]
    assert len(windows) == 2
    for window in windows:
        assert set(window) == set(WINDOW_KEYS)

    assert windows[0] == {
        "key": "primary",
        "label": "5h",
        "used_percent": 53.0,
        "resets_at": RESETS_AT_ISO,
        "model": None,
    }
    assert windows[1] == {
        "key": "secondary",
        "label": "weekly",
        "used_percent": 51.0,
        "resets_at": RESETS_AT_ISO,
        "model": None,
    }


def test_probe_resets_at_epoch_converts_to_iso_utc(codex_home: Path):
    write_rollout(
        codex_home,
        "session-a",
        "rollout-a.jsonl",
        [
            _token_count_line(
                CODEX_TS,
                limit_id="codex",
                primary=_primary(10, 300, RESETS_AT_EPOCH),
                secondary=_primary(20, 10080, RESETS_AT_EPOCH),
            ),
        ],
    )

    result = probe(codex_home)

    assert result["available"] is True
    assert result["windows"][0]["resets_at"] == RESETS_AT_ISO
    assert result["windows"][0]["resets_at"].endswith("+00:00")
    assert result["windows"][1]["resets_at"] == RESETS_AT_ISO


def test_probe_last_codex_line_wins_within_file(codex_home: Path):
    write_rollout(
        codex_home,
        "session-a",
        "rollout-a.jsonl",
        [
            _token_count_line(
                "2026-09-01T00:00:00+00:00",
                limit_id="codex",
                primary=_primary(90, 300),
                secondary=_primary(80, 10080),
                ordinal=1,
            ),
            _token_count_line(
                CODEX_TS,
                limit_id="codex",
                primary=_primary(12, 300),
                secondary=_primary(34, 10080),
                ordinal=2,
            ),
        ],
    )

    result = probe(codex_home)

    assert result["available"] is True
    assert result["as_of"] == CODEX_TS
    assert result["windows"][0]["used_percent"] == 12.0
    assert result["windows"][1]["used_percent"] == 34.0


def test_probe_newer_file_mtime_wins_over_older_file(codex_home: Path):
    older_mtime = 1_700_000_000.0
    newer_mtime = 1_800_000_000.0
    write_rollout(
        codex_home,
        "older-session",
        "rollout-older.jsonl",
        [
            _token_count_line(
                "2026-09-23T23:59:59+00:00",
                limit_id="codex",
                primary=_primary(99, 300),
                secondary=_primary(99, 10080),
            ),
        ],
        mtime=older_mtime,
    )
    write_rollout(
        codex_home,
        "newer-session",
        "rollout-newer.jsonl",
        [
            _token_count_line(
                "2026-09-01T00:00:00+00:00",
                limit_id="codex",
                primary=_primary(7, 300),
                secondary=_primary(8, 10080),
            ),
        ],
        mtime=newer_mtime,
    )

    result = probe(codex_home)

    assert result["available"] is True
    assert result["as_of"] == "2026-09-01T00:00:00+00:00"
    assert result["windows"][0]["used_percent"] == 7.0
    assert result["windows"][1]["used_percent"] == 8.0


def test_probe_tail_read_parses_codex_event_after_large_prefix(codex_home: Path):
    filler = json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "ordinal": 0,
            "type": "event",
            "payload": {"type": "other", "note": "noise"},
        }
    )
    prefix_bytes = 2 * 1024 * 1024
    filler_line = filler + " " * max(0, prefix_bytes - len(filler) - 1)
    codex_line = _token_count_line(
        CODEX_TS,
        limit_id="codex",
        primary=_primary(44, 300),
        secondary=_primary(55, 10080),
    )
    write_rollout(
        codex_home,
        "large-session",
        "rollout-large.jsonl",
        [filler_line, codex_line],
    )

    result = probe(codex_home)

    assert result["available"] is True
    assert result["as_of"] == CODEX_TS
    assert result["windows"][0]["used_percent"] == 44.0
    assert result["windows"][1]["used_percent"] == 55.0


def test_probe_missing_sessions_dir_returns_no_local_sample(codex_home: Path):
    codex_home.mkdir()

    result = probe(codex_home)

    assert set(result) == set(HARNESS_LIMITS_KEYS)
    assert result["harness"] == "codex"
    assert result["installed"] is True
    assert result["available"] is False
    assert result["windows"] == []
    assert result["error"] == "no local sample"
