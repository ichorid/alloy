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


# -- real overnight messages (tentura .alloy/logs, 2026-10-06) ---------------
# 134 fast-failed calls carried these and none was flagged unavailable, so each
# one burned agent-call budget: codex says "try again at", cursor names a date.

from alloy.limits.retry import DEFAULT_BREAKER_WAIT, parse_limit  # noqa: E402
from alloy.runners.base import _failure_message  # noqa: E402
from alloy.runners.codex import CodexRunner  # noqa: E402

CODEX_STDOUT = (
    '{"type":"thread.started","thread_id":"01a11384-4428-74e0-94b4-68bc252ceceb"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"error","message":"You\\u2019ve hit your usage limit. Upgrade to Pro '
    "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage to purchase "
    'more credits or try again at 2:31 AM."}\n'
    '{"type":"turn.failed","error":{"message":"You\\u2019ve hit your usage limit. Upgrade to Pro '
    "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage to purchase "
    'more credits or try again at 2:31 AM."}}\n'
)
CODEX_STDERR = "Reading additional input from stdin...\n"
CURSOR_STDERR = (
    "ActionRequiredError: You've hit your usage limit You've saved $255 on API model usage this "
    "month with Pro. Switch to a different model or set a Spend Limit to continue with this model. "
    "Your usage limits will reset when your monthly cycle ends on 10/26/2026.\n"
)
CURSOR_RECONNECT_STDERR = (
    "Connection lost, reconnecting to https://agentn.global.api5.cursor.sh (attempt 1)...\n"
    "Retry attempt 1...\n" + CURSOR_STDERR
)


def _codex_error() -> str:
    text, _structured, _usage, _session, failed = CodexRunner().parse(CODEX_STDOUT, CODEX_STDERR, 1)
    assert failed
    return _failure_message(text, CODEX_STDERR, 1)


def test_codex_usage_limit_try_again_at_is_a_retry_time():
    now = datetime(2026, 10, 7, 2, 19)
    assert parse_retry_at(_codex_error(), now=now) == datetime(2026, 10, 7, 2, 31)


def test_codex_usage_limit_try_again_at_rolls_to_the_next_day():
    now = datetime(2026, 10, 7, 9, 0)
    assert parse_retry_at(_codex_error(), now=now) == datetime(2026, 10, 8, 2, 31)


def test_cursor_usage_limit_resets_at_the_end_of_the_monthly_cycle():
    now = datetime(2026, 10, 7, 1, 47)
    error = _failure_message("", CURSOR_STDERR, 1)
    assert parse_retry_at(error, now=now) == datetime(2026, 10, 26)
    assert parse_retry_at(_failure_message("", CURSOR_RECONNECT_STDERR, 1), now=now) == datetime(2026, 10, 26)


def test_parse_limit_codex_is_harness_wide_with_a_parsed_reset():
    now = datetime(2026, 10, 7, 2, 19)
    hit = parse_limit(_codex_error(), now=now)
    assert hit is not None and hit.strong
    assert hit.reset_at == datetime(2026, 10, 7, 2, 31)
    assert not hit.model_scoped
    assert "usage limit" in hit.reason


def test_parse_limit_cursor_is_model_scoped():
    now = datetime(2026, 10, 7, 1, 47)
    hit = parse_limit(_failure_message("", CURSOR_STDERR, 1), now=now)
    assert hit is not None
    assert hit.model_scoped
    assert hit.reset_at == datetime(2026, 10, 26)


def test_parse_limit_without_a_reset_time_waits_the_default_breaker_window():
    now = datetime(2026, 10, 7, 12, 0)
    hit = parse_limit("Your workspace is out of credits.", now=now)
    assert hit is not None and hit.reset_at is None
    assert hit.until(now) == now + DEFAULT_BREAKER_WAIT == now + timedelta(hours=3)


def test_parse_limit_ignores_ordinary_failures():
    now = datetime(2026, 10, 7, 12, 0)
    assert parse_limit("timed out after 900s", now=now) is None
    assert parse_limit("AssertionError: expected 3, got 4\nretry after 5 seconds", now=now) is None
    assert parse_limit("", now=now) is None
