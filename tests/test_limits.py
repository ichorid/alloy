"""Harness limit parsing for timed auto-resume (alloy-5wb.4)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

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


# -- full named dates (tentura runner_breaker row, 2026-10-08: parsed=0) ------
# codex named a reset days away; the time-only pattern missed it and the
# breaker fell back to the 3-hour default, probing a dead harness repeatedly.

CODEX_DATED = (
    "You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
    "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at Oct 14th, 2026 5:31 AM."
)
NOW = datetime(2026, 10, 8, 12, 0)


def test_codex_dated_reset_with_typographic_apostrophe_is_parsed():
    assert "’" in CODEX_DATED
    hit = parse_limit(CODEX_DATED, now=NOW)
    assert hit is not None and hit.strong and not hit.model_scoped
    assert hit.reset_at == datetime(2026, 10, 14, 5, 31)
    assert hit.until(NOW) == datetime(2026, 10, 14, 5, 31)  # days ahead are honoured, not clamped
    assert parse_retry_at(CODEX_DATED, now=NOW) == datetime(2026, 10, 14, 5, 31)


def test_codex_dated_reset_keeps_an_aware_clock_aware():
    from datetime import timezone

    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    assert parse_limit(CODEX_DATED, now=now).reset_at == datetime(2026, 10, 14, 5, 31, tzinfo=now.tzinfo)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("try again at Oct 14th, 2026 5:31 AM", datetime(2026, 10, 14, 5, 31)),
        ("try again at Oct 14th 5:31 AM", datetime(2026, 10, 14, 5, 31)),
        ("try again at October 14, 5:31 AM", datetime(2026, 10, 14, 5, 31)),
        ("try again at Oct 14th, 2026 5:31 PM", datetime(2026, 10, 14, 17, 31)),
        ("try again at Oct 14, 2026 17:31", datetime(2026, 10, 14, 17, 31)),
        ("try again on Nov 1st at 9:05 am", datetime(2026, 11, 1, 9, 5)),
        ("try again on November 2nd, 2026 12:00 AM", datetime(2026, 11, 2, 0, 0)),
        ("try again on Dec 3rd 12:15 PM", datetime(2026, 12, 3, 12, 15)),
        ("resets Sept 23rd 8 am", datetime(2027, 9, 23, 8, 0)),  # already past this year: next year
        ("try again at Oct 8th 11:00 AM", datetime(2027, 10, 8, 11, 0)),  # earlier today: next year
        ("try again on Oct 20th", datetime(2026, 10, 20)),  # date only: local midnight
        ("try again at Thursday, Oct 15th 5:31 AM", datetime(2026, 10, 15, 5, 31)),
    ],
)
def test_named_month_reset_times(phrase, expected):
    hit = parse_limit(f"You’ve hit your usage limit. {phrase}.", now=NOW)
    assert hit is not None
    assert hit.reset_at == expected


def test_named_month_reset_with_a_timezone_abbreviation():
    from datetime import timezone

    utc_now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    for phrase, expected in [
        ("try again at Oct 14th, 2026 5:31 AM UTC", datetime(2026, 10, 14, 5, 31, tzinfo=timezone.utc)),
        ("try again at Oct 14th, 2026 5:31 AM PDT", datetime(2026, 10, 14, 12, 31, tzinfo=timezone.utc)),
        ("try again at Oct 14th 17:31 (CEST)", datetime(2026, 10, 14, 15, 31, tzinfo=timezone.utc)),
        ("try again at Oct 14th 17:31 UTC+3", datetime(2026, 10, 14, 14, 31, tzinfo=timezone.utc)),
    ]:
        hit = parse_limit(f"You've hit your usage limit. {phrase}.", now=utc_now)
        assert hit is not None and hit.reset_at == expected, phrase


@pytest.mark.parametrize(
    "phrase",
    [
        "try again at Oct 1st, 2026 5:31 AM",  # past date: nonsense, not a reset
        "try again at Feb 30th 5:31 AM",  # no such day
        "try again at Oct 14th, 2031 5:31 AM",  # years away
        "try again at Oct 14th 13:31 PM",  # no such 12h time
    ],
)
def test_nonsense_named_dates_are_unparsed(phrase):
    hit = parse_limit(f"You've hit your usage limit. {phrase}.", now=NOW)
    assert hit is not None and hit.reset_at is None
    assert hit.until(NOW) == NOW + DEFAULT_BREAKER_WAIT


def test_the_earlier_real_messages_still_parse():
    assert parse_limit("You've hit your usage limit. try again at 2:31 AM.", now=NOW).reset_at == datetime(
        2026, 10, 9, 2, 31
    )
    assert parse_limit("You've hit your usage limit. try again at 3:40 PM.", now=NOW).reset_at == datetime(
        2026, 10, 8, 15, 40
    )
    assert parse_limit(_failure_message("", CURSOR_STDERR, 1), now=NOW).reset_at == datetime(2026, 10, 26)


@pytest.mark.parametrize(
    "error",
    [
        "rate limit exceeded",
        "429 Too Many Requests: rate_limit_error, please slow down",
        "Implemented the rate limit middleware for October; tests pass on Oct 14th",
        "You've hit your usage limit.",
    ],
)
def test_limit_chatter_without_a_time_is_unparsed(error):
    hit = parse_limit(error, now=NOW)
    assert hit is not None and hit.reset_at is None
    assert parse_retry_at(error, now=NOW) == NOW + timedelta(minutes=30)
