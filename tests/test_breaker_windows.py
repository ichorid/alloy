"""The runner breaker knows WHICH window (5h / weekly / monthly) blocks a
harness and recovers early when that window resets (2026-10-08/09, tentura):

* codex said "try again at Oct 14th, 2026 5:31 AM" -- the exhausted weekly
  window -- while earlier messages named the 5-hour one ("2:31 AM");
* "1:57 AM" read at 01:57:27 kept codex out until the next night, although
  the rollout showed the 5-hour window resetting at 01:57:33;
* the owner reset limits by hand and had to delete runner_breaker rows.

Rate-limit fixtures are the real codex session-rollout `token_count` shapes
(limit_id/primary/secondary/used_percent/window_minutes/resets_at)."""

from __future__ import annotations

import json
import logging
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from support import load_config, make_bead
from test_limits import CURSOR_STDERR
from test_runner_breaker import ScriptedRunner, _Registry

from alloy.config import RoleSpec
from alloy.limits import breaker
from alloy.limits.breaker import Availability, read_state, recovery_verdict, resolve
from alloy.limits.retry import parse_limit, parse_retry_at
from alloy.models import utcnow
from alloy.runners.base import _failure_message
from alloy.runtime import RunContext
from alloy.store import Store

UTC = timezone.utc


def codex_message(when: str, settings: str = "codex/settings") -> str:
    """The real codex usage-limit wording (typographic apostrophe)."""
    return (
        "You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
        f"https://chatgpt.com/{settings}/usage to purchase more credits or try again at {when}."
    )


WEEKLY_MESSAGE = codex_message("Oct 14th, 2026 5:31 AM")
SHORT_MESSAGE = codex_message("2:31 AM")


def rate_limits(primary: tuple[float, int], secondary: tuple[float, int]) -> dict:
    """A real `rate_limits` payload: (used_percent, resets_at epoch) per window."""
    return {
        "limit_id": "codex",
        "limit_name": None,
        "primary": {"used_percent": primary[0], "window_minutes": 300, "resets_at": primary[1]},
        "secondary": {"used_percent": secondary[0], "window_minutes": 10080, "resets_at": secondary[1]},
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        "individual_limit": None,
        "spend_control_reached": None,
        "plan_type": "plus",
        "rate_limit_reached_type": None,
    }


def token_count(timestamp: datetime, limits: dict) -> str:
    return json.dumps(
        {
            "timestamp": timestamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {}, "rate_limits": limits},
        }
    )


PREMIUM = {
    "limit_id": "premium",
    "limit_name": None,
    "primary": None,
    "secondary": None,
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    "individual_limit": None,
    "spend_control_reached": None,
    "plan_type": "plus",
    "rate_limit_reached_type": None,
}


def write_rollout(home, name: str, *events: tuple[datetime, dict]) -> None:
    day = home / ".codex" / "sessions" / "2026" / "10" / "08"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"rollout-{name}.jsonl").write_text("\n".join(token_count(t, rl) for t, rl in events) + "\n")


def codex_sample(as_of: datetime, primary: tuple[float, datetime], secondary: tuple[float, datetime]) -> dict:
    """What `alloy.limits.codex.probe` returns for one token_count."""
    return {
        "harness": "codex",
        "installed": True,
        "available": True,
        "fetched_at": as_of.isoformat(),
        "as_of": as_of.astimezone(UTC).isoformat(),
        "source": "session-rollout",
        "error": None,
        "status": None,
        "windows": [
            {
                "key": "primary",
                "label": "5h",
                "used_percent": primary[0],
                "resets_at": primary[1].isoformat(),
                "model": None,
            },
            {
                "key": "secondary",
                "label": "weekly",
                "used_percent": secondary[0],
                "resets_at": secondary[1].isoformat(),
                "model": None,
            },
        ],
    }


def epoch(value: datetime) -> int:
    return int(value.timestamp())


# -- real 2026-10-08 timeline (CEST = UTC+2) ---------------------------------

CEST = timezone(timedelta(hours=2))
OCT8_FAIL = datetime(2026, 10, 8, 13, 18, 36, tzinfo=CEST)  # the weekly failure
OCT8_PRIMARY_RESET = datetime(2026, 10, 8, 17, 17, 21, tzinfo=CEST)  # 1791472641
OCT14_WEEKLY_RESET = datetime(2026, 10, 14, 5, 31, 7, tzinfo=CEST)  # 1791948667


# -- message parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "now", "window"),
    [
        (SHORT_MESSAGE, datetime(2026, 10, 7, 1, 19), "5h"),
        (codex_message("3:40 PM"), datetime(2026, 10, 7, 13, 44), "5h"),
        (WEEKLY_MESSAGE, datetime(2026, 10, 8, 13, 18), "weekly"),
        (CURSOR_STDERR, datetime(2026, 10, 7, 14, 17), "month"),
        ("You've hit your session limit · resets 1:20am (Europe/Amsterdam)", datetime(2026, 9, 22, 0, 44), "5h"),
        ("You've hit your usage limit. retry after 90 seconds", datetime(2026, 9, 22, 0, 44), "unknown"),
        ("Your workspace is out of credits.", datetime(2026, 9, 22, 0, 44), "unknown"),
    ],
)
def test_message_time_is_labelled_by_wording_and_magnitude(error, now, window):
    assert parse_limit(error, now).window == window


def test_a_bare_time_read_in_its_own_minute_is_now_not_tomorrow():
    # 2026-10-07 15:40:26: "3:40 PM" (the window reset at 15:40:42) used to
    # become 2026-10-08 15:40; 2026-10-09 01:57:27 "1:57 AM" became 10-10.
    now = datetime(2026, 10, 7, 15, 40, 26)
    assert parse_limit(codex_message("3:40 PM"), now).reset_at == datetime(2026, 10, 7, 15, 41, 26)
    now = datetime(2026, 10, 9, 1, 57, 27)
    assert parse_retry_at(codex_message("1:57 AM"), now) == datetime(2026, 10, 9, 1, 58, 27)
    # Still the next day when the time is well past (e.g. read at 3 am).
    assert parse_retry_at(codex_message("1:57 AM"), datetime(2026, 10, 9, 3, 0)) == datetime(2026, 10, 10, 1, 57)


# -- resolve: message x window data -------------------------------------------


def test_primary_exhausted_secondary_fine_blocks_until_the_5h_reset():
    now = datetime(2026, 10, 7, 1, 19, 57, tzinfo=CEST)
    primary_reset = datetime(2026, 10, 7, 2, 31, 11, tzinfo=CEST)  # 1791333071
    sample = codex_sample(now - timedelta(minutes=3), (100.0, primary_reset), (51.0, OCT14_WEEKLY_RESET))
    hit = parse_limit(SHORT_MESSAGE, now.replace(tzinfo=None))

    available = resolve(hit, sample, failed_at=now, now=now)

    assert available == Availability(primary_reset, "5h", "window", ("primary",))


def test_secondary_exhausted_blocks_until_the_weekly_reset():
    sample = codex_sample(OCT8_FAIL + timedelta(minutes=5), (42.0, OCT8_PRIMARY_RESET), (100.0, OCT14_WEEKLY_RESET))
    hit = parse_limit(WEEKLY_MESSAGE, OCT8_FAIL.replace(tzinfo=None))

    available = resolve(hit, sample, failed_at=OCT8_FAIL, now=OCT8_FAIL + timedelta(minutes=6))

    assert available == Availability(OCT14_WEEKLY_RESET, "weekly", "window", ("secondary",))


def test_both_windows_exhausted_take_the_later_reset():
    now = OCT8_FAIL
    sample = codex_sample(now + timedelta(seconds=30), (100.0, OCT8_PRIMARY_RESET), (100.0, OCT14_WEEKLY_RESET))
    hit = parse_limit(codex_message("5:17 PM"), now.replace(tzinfo=None))  # names the 5h window only

    available = resolve(hit, sample, failed_at=now, now=now)

    assert available.until == OCT14_WEEKLY_RESET
    assert (available.window, available.source) == ("weekly", "window")
    assert available.window_keys == ("primary", "secondary")


def test_both_exhausted_without_a_message_time_take_the_later_reset():
    now = OCT8_FAIL
    sample = codex_sample(now - timedelta(minutes=1), (100.0, OCT8_PRIMARY_RESET), (100.0, OCT14_WEEKLY_RESET))
    hit = parse_limit("Your workspace is out of credits.", now.replace(tzinfo=None))

    assert resolve(hit, sample, failed_at=now, now=now).until == OCT14_WEEKLY_RESET


def test_message_wins_over_window_data_older_than_the_failure():
    # 2026-10-08 19:16: the owner had reset the weekly window by hand; the last
    # rollout sample (11:23) still showed it at 100% until Oct 14, but codex
    # now said "try again at 8:56 PM".
    now = datetime(2026, 10, 8, 19, 16, 33, tzinfo=CEST)
    stale = codex_sample(
        datetime(2026, 10, 8, 13, 23, 26, tzinfo=CEST), (42.0, OCT8_PRIMARY_RESET), (100.0, OCT14_WEEKLY_RESET)
    )
    hit = parse_limit(codex_message("8:56 PM", "settings"), now.replace(tzinfo=None))

    available = resolve(hit, stale, failed_at=now, now=now)

    assert available.until == datetime(2026, 10, 8, 20, 56, tzinfo=CEST)
    assert (available.window, available.source) == ("5h", "message")


def test_window_data_fresher_than_the_failure_wins_over_a_disagreeing_message():
    now = OCT8_FAIL
    fresh = codex_sample(now + timedelta(minutes=2), (40.0, OCT8_PRIMARY_RESET), (100.0, OCT14_WEEKLY_RESET))
    hit = parse_limit(codex_message("2:31 PM"), now.replace(tzinfo=None))  # matches no window

    available = resolve(hit, fresh, failed_at=now, now=now + timedelta(minutes=3))

    assert available == Availability(OCT14_WEEKLY_RESET, "weekly", "window", ("secondary",))


def test_message_only_and_nothing_known():
    now = OCT8_FAIL
    weekly = resolve(parse_limit(WEEKLY_MESSAGE, now.replace(tzinfo=None)), None, failed_at=now, now=now)
    assert (weekly.window, weekly.source) == ("weekly", "message")
    assert weekly.until == datetime(2026, 10, 14, 5, 31).astimezone()

    cursor = resolve(parse_limit(CURSOR_STDERR, now.replace(tzinfo=None)), None, failed_at=now, now=now)
    assert (cursor.window, cursor.source) == ("month", "message")
    assert cursor.until == datetime(2026, 10, 26).astimezone()

    unknown = resolve(parse_limit("Your workspace is out of credits.", now), None, failed_at=now, now=now)
    assert unknown == Availability(now + timedelta(hours=3), "unknown", "default")


def test_read_state_takes_the_newest_codex_rollout(tmp_path, monkeypatch):
    monkeypatch.delenv("ALLOY_FAKE_CONFIG", raising=False)
    sample_at = datetime(2026, 10, 8, 13, 23, 26, tzinfo=CEST)
    write_rollout(
        tmp_path,
        "2026-10-08T13-18-36",
        (sample_at, rate_limits((42.0, epoch(OCT8_PRIMARY_RESET)), (100.0, epoch(OCT14_WEEKLY_RESET)))),
        (sample_at + timedelta(seconds=31), PREMIUM),  # what a failing call leaves behind
    )

    sample = read_state("codex", cache_path=None, home=tmp_path)
    hit = parse_limit(WEEKLY_MESSAGE, OCT8_FAIL.replace(tzinfo=None))

    assert resolve(hit, sample, failed_at=OCT8_FAIL, now=sample_at + timedelta(minutes=1)) == Availability(
        OCT14_WEEKLY_RESET, "weekly", "window", ("secondary",)
    )


def test_read_state_is_off_in_fake_harness_mode(tmp_path, monkeypatch):
    write_rollout(
        tmp_path, "x", (OCT8_FAIL, rate_limits((1.0, epoch(OCT8_PRIMARY_RESET)), (1.0, epoch(OCT14_WEEKLY_RESET))))
    )
    monkeypatch.setenv("ALLOY_FAKE_CONFIG", "fake.yaml")
    assert read_state("codex", cache_path=None, home=tmp_path) is None


def test_read_state_falls_back_to_the_limits_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("ALLOY_FAKE_CONFIG", raising=False)
    cache = tmp_path / "limits.json"
    cache.write_text(json.dumps({"claude": {"available": True, "as_of": "2026-10-08T10:00:00+00:00", "windows": []}}))
    assert read_state("claude", cache_path=cache, home=tmp_path)["as_of"] == "2026-10-08T10:00:00+00:00"
    assert read_state("cursor", cache_path=cache, home=tmp_path) is None


def test_recovery_verdict():
    marked = OCT8_FAIL
    later = marked + timedelta(hours=4)
    reset = codex_sample(later, (3.0, later + timedelta(hours=5)), (100.0, OCT14_WEEKLY_RESET))
    # only the blocking window counts
    assert recovery_verdict(reset, window="5h", window_keys=("primary",), marked_at=marked, now=later) == "reset"
    assert (
        recovery_verdict(reset, window="weekly", window_keys=("secondary",), marked_at=marked, now=later) == "blocked"
    )
    # message-only mark: watched by window kind
    assert recovery_verdict(reset, window="5h", window_keys=(), marked_at=marked, now=later) == "reset"
    # a sample older than the mark says nothing
    old = codex_sample(marked - timedelta(minutes=1), (3.0, later), (3.0, later))
    assert recovery_verdict(old, window="5h", window_keys=("primary",), marked_at=marked, now=later) is None
    assert recovery_verdict(None, window="5h", window_keys=("primary",), marked_at=marked, now=later) is None


# -- runtime: marking, auto-recovery, probe cadence ---------------------------


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class States:
    """A fake `limit_state` reader that counts reads."""

    def __init__(self, sample: dict | None = None) -> None:
        self.sample = sample
        self.reads = 0

    def __call__(self, harness: str) -> dict | None:
        self.reads += 1
        return self.sample


CHAIN = RoleSpec.parse({"runner": "codex", "model": "gpt-6.1-sol", "fallback": {"runner": "claude-write"}})


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "alloy.db")
    store.create_run(
        run_id="run-1",
        bead_id="t-1",
        thread_id="run-1",
        recipe="tdd-loop",
        repo=tmp_path,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    return store


def _ctx(store, tmp_path, runners, *, clock, states=None, seed=7) -> RunContext:
    return RunContext(
        bead=make_bead(),
        recipe=load_config(),
        run_id="run-1",
        worktree=SimpleNamespace(path=tmp_path),
        worktrees=None,
        registry=_Registry(runners),
        store=store,
        checkpointer=None,
        log_dir=tmp_path,
        limit_state=states,
        clock=clock,
        probe_rng=random.Random(seed),
    )


def _short_hit_setup():
    """A codex 5h limit 'now': primary at 100% resetting in 3 h (the message
    names that minute), secondary fine."""
    t0 = utcnow().replace(microsecond=0)
    reset = t0 + timedelta(hours=3, seconds=11)
    message = codex_message(reset.astimezone().strftime("%I:%M %p").lstrip("0"))
    sample = codex_sample(t0 - timedelta(minutes=2), (100.0, reset), (51.0, t0 + timedelta(days=5)))
    return t0, reset, message, sample


async def test_a_limit_is_marked_with_the_window_and_its_source(store, tmp_path):
    t0, reset, message, sample = _short_hit_setup()
    codex = ScriptedRunner("codex", (False, message, 2.0))
    ctx = _ctx(
        store,
        tmp_path,
        {"codex": codex, "claude-write": ScriptedRunner("claude-write")},
        clock=Clock(t0),
        states=States(sample),
    )

    await ctx.call("implement", CHAIN, "prompt")

    (entry,) = store.runners_unavailable(now=t0)
    assert (entry["window"], entry["source"], entry["reset_parsed"]) == ("5h", "window", True)
    assert datetime.fromisoformat(entry["until"]) == reset  # the window's exact second
    assert entry["next_probe_at"] is not None


async def test_breaker_clears_early_when_the_blocking_window_resets(store, tmp_path, caplog):
    t0, reset, message, sample = _short_hit_setup()
    clock = Clock(t0)
    states = States(sample)
    codex = ScriptedRunner("codex", (False, message, 2.0), (True, None, 1.0))
    claude = ScriptedRunner("claude-write")
    ctx = _ctx(store, tmp_path, {"codex": codex, "claude-write": claude}, clock=clock, states=states)
    await ctx.call("implement", CHAIN, "prompt")
    reads_after_mark = states.reads

    clock.advance(minutes=2)
    await ctx.call("implement", CHAIN, "prompt")
    assert (codex.calls, claude.calls) == (1, 2)
    assert states.reads == reads_after_mark  # not re-read within CHECK_INTERVAL

    # The owner resets the limit; a codex session elsewhere logs the new state.
    clock.advance(minutes=4)
    states.sample = codex_sample(
        clock.now - timedelta(seconds=20), (4.0, clock.now + timedelta(hours=5)), (51.0, t0 + timedelta(days=5))
    )
    with caplog.at_level(logging.WARNING, logger="alloy.runtime"):
        await ctx.call("implement", CHAIN, "prompt")

    assert codex.calls == 2 and claude.calls == 2
    assert store.runners_unavailable(now=clock.now) == []
    assert "breaker cleared early: 5h reset detected" in caplog.text


async def test_fresh_state_still_exhausted_keeps_the_mark_and_defers_the_probe(store, tmp_path):
    t0, reset, message, sample = _short_hit_setup()
    clock = Clock(t0)
    states = States(sample)
    codex = ScriptedRunner("codex", (False, message, 2.0))
    ctx = _ctx(
        store, tmp_path, {"codex": codex, "claude-write": ScriptedRunner("claude-write")}, clock=clock, states=states
    )
    await ctx.call("implement", CHAIN, "prompt")
    (before,) = store.runners_unavailable(now=clock.now)

    clock.advance(minutes=29)
    states.sample = codex_sample(clock.now, (100.0, reset), (51.0, t0 + timedelta(days=5)))
    await ctx.call("implement", CHAIN, "prompt")
    clock.advance(minutes=9)  # past the original probe time
    await ctx.call("implement", CHAIN, "prompt")

    assert codex.calls == 1  # never probed: the fresh state says it is still exhausted
    (after,) = store.runners_unavailable(now=clock.now)
    assert after["checked_at"] is not None
    assert datetime.fromisoformat(after["next_probe_at"]) > datetime.fromisoformat(before["next_probe_at"])


async def test_probe_cadence_without_readable_state(store, tmp_path):
    t0 = utcnow().replace(microsecond=0)
    clock = Clock(t0)
    # cursor's model-scoped monthly limit: nothing readable, days away
    cursor = ScriptedRunner(
        "cursor",
        (False, _failure_message("", CURSOR_STDERR, 1), 3.0),
        (False, _failure_message("", CURSOR_STDERR, 1), 3.0),
        (True, None, 1.0),
    )
    claude = ScriptedRunner("claude-write")
    kimi = RoleSpec.parse({"runner": "cursor", "model": "kimi-k3-high", "fallback": {"runner": "claude-write"}})
    ctx = _ctx(store, tmp_path, {"cursor": cursor, "claude-write": claude}, clock=clock)

    await ctx.call("implement", kimi, "prompt")
    (entry,) = store.runners_unavailable(now=clock.now)
    assert (entry["model"], entry["window"], entry["source"]) == ("kimi-k3-high", "month", "message")
    due = datetime.fromisoformat(entry["next_probe_at"])
    assert breaker.PROBE_INTERVAL * 0.8 <= due - t0 <= breaker.PROBE_INTERVAL * 1.2

    clock.now = due - timedelta(seconds=1)
    await ctx.call("implement", kimi, "prompt")
    assert cursor.calls == 1  # not yet due: skipped

    clock.now = due
    await ctx.call("implement", kimi, "prompt")
    assert cursor.calls == 2  # one probe; it hit the limit again and re-marked
    await ctx.call("implement", kimi, "prompt")
    assert cursor.calls == 2  # the next probe is a fresh interval away
    (again,) = store.runners_unavailable(now=clock.now)
    next_due = datetime.fromisoformat(again["next_probe_at"])
    assert next_due - clock.now >= breaker.PROBE_INTERVAL * 0.8

    clock.now = next_due  # the owner reset it in between: the probe works
    result = await ctx.call("implement", kimi, "prompt")
    assert result.ok and cursor.calls == 3
    assert store.runners_unavailable(now=clock.now) == []
    assert claude.calls == 4  # every non-probe call went to the fallback


async def test_concurrent_calls_claim_one_probe(store, tmp_path):
    t0 = utcnow().replace(microsecond=0)
    clock = Clock(t0)
    store.mark_runner_unavailable(
        "codex",
        t0 + timedelta(days=3),
        reason="usage limit",
        window="weekly",
        source="message",
        next_probe_at=t0,
        now=t0,
    )
    ctx = _ctx(store, tmp_path, {}, clock=clock)
    entries = store.runner_breaker_entries("codex", "gpt-6.1-sol", now=t0)

    assert ctx._claim_probe(entries) is True
    assert ctx._claim_probe(entries) is False  # stale view: someone already claimed it
    assert ctx._claim_probe(store.runner_breaker_entries("codex", now=t0)) is False  # not due again yet


async def test_old_rows_are_probed_half_an_hour_after_marking(store, tmp_path):
    t0 = utcnow().replace(microsecond=0)
    store.mark_runner_unavailable("codex", t0 + timedelta(days=3), reason="usage limit", now=t0)
    ctx = _ctx(store, tmp_path, {}, clock=Clock(t0 + timedelta(minutes=29)))
    assert ctx._claim_probe(store.runner_breaker_entries("codex", now=ctx.clock())) is False
    ctx.clock.advance(minutes=1)
    assert ctx._claim_probe(store.runner_breaker_entries("codex", now=ctx.clock())) is True


async def test_no_probe_when_the_window_ends_before_the_next_probe(store, tmp_path):
    t0 = utcnow().replace(microsecond=0)
    store.mark_runner_unavailable(
        "codex", t0 + timedelta(minutes=10), reason="x", next_probe_at=t0 + timedelta(minutes=30), now=t0
    )
    ctx = _ctx(store, tmp_path, {}, clock=Clock(t0 + timedelta(minutes=5)))
    assert ctx._claim_probe(store.runner_breaker_entries("codex", now=ctx.clock())) is False


# -- store: old rows, status ---------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE runner_breaker (
    harness      TEXT NOT NULL,
    model        TEXT NOT NULL DEFAULT '',
    until        TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    parsed       INTEGER NOT NULL DEFAULT 0,
    marked_at    TEXT NOT NULL,
    PRIMARY KEY (harness, model)
);
"""


def test_old_runner_breaker_rows_gain_window_and_source(tmp_path):
    path = tmp_path / "alloy.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    now = utcnow()
    rows = [
        # the real tentura row (2026-10-07)
        (
            "cursor",
            "kimi-k3-high",
            (now + timedelta(days=17)).isoformat(),
            CURSOR_STDERR.strip(),
            1,
            (now - timedelta(days=2)).isoformat(),
        ),
        (
            "codex",
            "",
            (now + timedelta(hours=2)).isoformat(),
            "You’ve hit your usage limit.",
            1,
            (now - timedelta(hours=1)).isoformat(),
        ),
        ("claude", "", (now + timedelta(hours=1)).isoformat(), "rate limit", 0, (now - timedelta(hours=2)).isoformat()),
        (
            "codex",
            "weekly-model",
            (now + timedelta(days=5)).isoformat(),
            "You’ve hit your usage limit.",
            1,
            now.isoformat(),
        ),
    ]
    conn.executemany("INSERT INTO runner_breaker VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    store = Store(path)

    entries = {(e["harness"], e["model"]): e for e in store.runners_unavailable()}
    assert (entries[("cursor", "kimi-k3-high")]["window"], entries[("cursor", "kimi-k3-high")]["source"]) == (
        "month",
        "message",
    )
    assert (entries[("codex", None)]["window"], entries[("codex", None)]["source"]) == ("5h", "message")
    assert (entries[("claude", None)]["window"], entries[("claude", None)]["source"]) == ("unknown", "default")
    assert entries[("codex", "weekly-model")]["window"] == "weekly"
    assert store.runner_unavailable_until("codex") is not None
    # new marks on the migrated table carry the new columns
    store.mark_runner_unavailable(
        "codex",
        now + timedelta(days=4),
        reason="x",
        parsed=True,
        window="weekly",
        source="window",
        window_keys=("secondary",),
    )
    (codex,) = store.runner_breaker_entries("codex")
    assert (codex["window"], codex["source"], codex["window_keys"]) == ("weekly", "window", ("secondary",))


def test_status_shows_window_and_source(beads_project, alloy_home):
    from typer.testing import CliRunner

    from alloy.cli import app
    from alloy.engine import Engine

    engine = Engine.open(beads_project, alloy_home)
    until = utcnow() + timedelta(days=4)
    engine.store.mark_runner_unavailable(
        "codex",
        until,
        reason="You’ve hit your usage limit.",
        parsed=True,
        window="weekly",
        source="window",
        window_keys=("secondary",),
    )
    engine.store.mark_runner_unavailable(
        "cursor", until, model="kimi-k3-high", reason="usage limit", parsed=True, window="month", source="message"
    )
    args = ["status", "--repo", str(beads_project), "--root", str(alloy_home)]

    result = CliRunner().invoke(app, [*args, "--json"])
    assert result.exit_code == 0, result.output
    entries = json.loads(result.stdout)["runners_unavailable"]
    assert [(e["harness"], e["window"], e["source"]) for e in entries] == [
        ("codex", "weekly", "window"),
        ("cursor", "month", "message"),
    ]

    text = CliRunner().invoke(app, args)
    assert text.exit_code == 0, text.output
    flat = " ".join(text.output.split())  # rich wraps at the console width
    assert "weekly window, from rate-limit state" in flat
    assert "month window, from the limit message" in flat
