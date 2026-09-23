"""Harness limit parsing for timed auto-resume (alloy-5wb.4)."""

from __future__ import annotations

from datetime import datetime, timedelta

from alloy.limits import parse_retry_at


def test_parse_retry_at_resets_am_pm_uses_next_local_occurrence():
    now = datetime(2026, 9, 22, 0, 44)
    result = parse_retry_at(
        "You've hit your session limit · resets 1:20am (Europe/Amsterdam)",
        now=now,
    )
    assert result == datetime(2026, 9, 22, 1, 20)


def test_parse_retry_at_retry_after_seconds():
    now = datetime(2026, 9, 22, 0, 44, 0)
    result = parse_retry_at("retry after 90 seconds", now=now)
    assert result == now + timedelta(seconds=90)


def test_parse_retry_at_retry_after_minutes():
    now = datetime(2026, 9, 22, 12, 0, 0)
    result = parse_retry_at("please retry after 5 minutes", now=now)
    assert result == now + timedelta(minutes=5)


def test_parse_retry_at_rate_limit_without_time_defaults_to_30_minutes():
    now = datetime(2026, 9, 22, 12, 0, 0)
    result = parse_retry_at("rate limit exceeded", now=now)
    assert result == now + timedelta(minutes=30)


def test_parse_retry_at_unrelated_error_returns_none():
    now = datetime(2026, 9, 22, 0, 44)
    assert parse_retry_at("file not found", now=now) is None
