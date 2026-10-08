"""Runner circuit breaker: a harness that reported a usage/spend/rate limit is
skipped until its reset time, straight to the role's fallback, without a
ledger row or budget spent (overnight 2026-10-06: 134 limit failures burned
the agent-call budget because nothing remembered the first one)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from support import load_config, make_bead
from test_limits import CODEX_STDERR, CODEX_STDOUT, CURSOR_STDERR

from alloy.config import RoleSpec
from alloy.models import AgentResult, is_unavailable, utcnow
from alloy.runners.base import _failure_message
from alloy.runners.codex import CodexRunner
from alloy.runtime import RunContext
from alloy.store import Store


def _codex_limit_error() -> str:
    text, *_ = CodexRunner().parse(CODEX_STDOUT, CODEX_STDERR, 1)
    return _failure_message(text, CODEX_STDERR, 1)


class ScriptedRunner:
    """Answers each call with the next scripted (ok, error, duration) entry."""

    def __init__(self, name: str, *answers: tuple[bool, str | None, float]) -> None:
        self.name = name
        self.answers = list(answers) or [(True, None, 0.01)]
        self.calls = 0

    async def run(self, prompt, cwd, *, model, timeout, structured_schema):
        ok, error, duration = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        now = utcnow()
        return AgentResult(
            runner=self.name,
            model=model,
            ok=ok,
            exit_code=0 if ok else (-1 if error and error.startswith("timed out") else 1),
            text="ok" if ok else "",
            started_at=now,
            ended_at=now,
            duration_s=duration,
            error=error,
        )


class _Registry:
    def __init__(self, runners: dict[str, Any]) -> None:
        self.runners = runners

    def get(self, name: str) -> Any:
        return self.runners[name]


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


def _ctx(store: Store, tmp_path, runners: dict[str, Any]) -> RunContext:
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
    )


CHAIN = RoleSpec.parse({"runner": "codex", "model": "gpt-6.1-sol", "fallback": {"runner": "claude-write"}})


async def test_codex_usage_limit_marks_the_harness_until_the_parsed_reset(store, tmp_path):
    codex = ScriptedRunner("codex", (False, _codex_limit_error(), 2.3))
    claude = ScriptedRunner("claude-write")
    ctx = _ctx(store, tmp_path, {"codex": codex, "claude-write": claude})

    result = await ctx.call("implement", CHAIN, "prompt")

    assert result.ok and claude.calls == 1
    first = store.agent_calls("run-1")[0]
    assert '"unavailable": true' in first["usage_json"]  # no budget spent on the limit
    entries = store.runners_unavailable()
    assert [(e["harness"], e["model"], e["reset_parsed"]) for e in entries] == [("codex", None, True)]
    until = datetime.fromisoformat(entries[0]["until"])
    local = until.astimezone()
    assert (local.hour, local.minute) == (2, 31)
    assert until > utcnow()


async def test_a_marked_harness_is_skipped_without_a_ledger_row(store, tmp_path):
    store.mark_runner_unavailable("codex", utcnow() + timedelta(hours=1), reason="usage limit")
    codex = ScriptedRunner("codex")
    claude = ScriptedRunner("claude-write")
    ctx = _ctx(store, tmp_path, {"codex": codex, "claude-write": claude})

    result = await ctx.call("implement", CHAIN, "prompt")

    assert result.ok
    assert codex.calls == 0 and claude.calls == 1
    assert [call["runner"] for call in store.agent_calls("run-1")] == ["claude-write"]
    assert store.call_count("run-1") == 1


async def test_codex_readonly_and_astra_share_the_codex_breaker(store, tmp_path):
    store.mark_runner_unavailable("codex", utcnow() + timedelta(hours=1), reason="usage limit")
    readonly = ScriptedRunner("codex-readonly")
    claude = ScriptedRunner("claude")
    ctx = _ctx(store, tmp_path, {"codex-readonly": readonly, "claude": claude})
    spec = RoleSpec.parse({"runner": "codex-readonly", "fallback": {"runner": "claude"}})

    await ctx.call("tests_review", spec, "prompt")

    assert readonly.calls == 0 and claude.calls == 1


async def test_when_every_runner_is_marked_the_last_one_is_probed(store, tmp_path):
    later = utcnow() + timedelta(hours=1)
    store.mark_runner_unavailable("codex", later, reason="usage limit")
    store.mark_runner_unavailable("claude", later, reason="session limit")
    codex = ScriptedRunner("codex")
    claude = ScriptedRunner("claude-write")
    ctx = _ctx(store, tmp_path, {"codex": codex, "claude-write": claude})

    result = await ctx.call("implement", CHAIN, "prompt")

    assert codex.calls == 0 and claude.calls == 1
    assert result.ok
    # The probe worked: the claude entry is gone, codex stays marked.
    assert [entry["harness"] for entry in store.runners_unavailable()] == ["codex"]


async def test_the_primary_is_used_again_once_the_window_passes(store, tmp_path):
    store.mark_runner_unavailable("codex", utcnow() - timedelta(seconds=1), reason="usage limit")
    codex = ScriptedRunner("codex")
    claude = ScriptedRunner("claude-write")
    ctx = _ctx(store, tmp_path, {"codex": codex, "claude-write": claude})

    await ctx.call("implement", CHAIN, "prompt")

    assert codex.calls == 1 and claude.calls == 0
    assert store.runners_unavailable() == []


async def test_no_reset_time_marks_for_three_hours_and_a_failed_retry_re_marks(store, tmp_path):
    codex = ScriptedRunner("codex", (False, "Your workspace is out of credits.", 1.0))
    claude = ScriptedRunner("claude-write")
    ctx = _ctx(store, tmp_path, {"codex": codex, "claude-write": claude})

    before = utcnow()
    await ctx.call("implement", CHAIN, "prompt")
    (entry,) = store.runners_unavailable()
    until = datetime.fromisoformat(entry["until"])
    assert entry["reset_parsed"] is False
    assert timedelta(hours=2, minutes=59) < until - before < timedelta(hours=3, minutes=1)

    # The window passes; one retry is allowed, fails the same way, re-marks.
    store.mark_runner_unavailable("codex", utcnow() - timedelta(seconds=1), reason=entry["reason"])
    await ctx.call("implement", CHAIN, "prompt")
    assert codex.calls == 2
    (again,) = store.runners_unavailable()
    assert datetime.fromisoformat(again["until"]) > utcnow() + timedelta(hours=2)


async def test_cursor_model_limit_blocks_only_that_model(store, tmp_path):
    cursor = ScriptedRunner("cursor", (False, _failure_message("", CURSOR_STDERR, 1), 3.1), (True, None, 0.01))
    claude = ScriptedRunner("claude-write")
    ctx = _ctx(store, tmp_path, {"cursor": cursor, "claude-write": claude})
    kimi = RoleSpec.parse({"runner": "cursor", "model": "kimi-k3-high", "fallback": {"runner": "claude-write"}})
    composer = RoleSpec.parse({"runner": "cursor", "model": "composer-2.5", "fallback": {"runner": "claude-write"}})

    await ctx.call("implement", kimi, "prompt")
    assert [(e["harness"], e["model"]) for e in store.runners_unavailable()] == [("cursor", "kimi-k3-high")]

    await ctx.call("implement", kimi, "prompt")  # skipped
    assert cursor.calls == 1
    await ctx.call("verifier", composer, "prompt")  # another model of the same harness still runs
    assert cursor.calls == 2


async def test_the_breaker_survives_a_store_reopen(store, tmp_path):
    codex = ScriptedRunner("codex", (False, _codex_limit_error(), 2.3))
    ctx = _ctx(store, tmp_path, {"codex": codex, "claude-write": ScriptedRunner("claude-write")})
    await ctx.call("implement", CHAIN, "prompt")

    reopened = Store(store.path)
    assert reopened.runner_unavailable_until("codex", "gpt-6-luna") is not None
    assert reopened.runner_unavailable_until("claude") is None


async def test_a_long_failed_call_that_only_mentions_rate_limits_does_not_trip_the_breaker(store, tmp_path):
    error = "Implemented the rate limit middleware but the test run failed: rate limit exceeded assertion"
    codex = ScriptedRunner("codex", (False, error, 600.0))
    ctx = _ctx(store, tmp_path, {"codex": codex, "claude-write": ScriptedRunner("claude-write")})

    await ctx.call("implement", CHAIN, "prompt")

    assert store.runners_unavailable() == []


async def test_a_timed_out_codex_attempt_falls_through_to_the_fallback(store, tmp_path):
    codex = ScriptedRunner("codex", (False, "timed out after 900s", 900.0))
    claude = ScriptedRunner("claude-write")
    ctx = _ctx(store, tmp_path, {"codex": codex, "claude-write": claude})

    result = await ctx.call("implement", CHAIN, "prompt")

    assert result.ok and claude.calls == 1
    assert store.runners_unavailable() == []  # a timeout is not a limit
    timed_out = store.agent_calls("run-1")[0]
    assert timed_out["exit_code"] == -1


def test_real_overnight_failures_are_unavailable():
    """The two real messages now set retry_at, so is_unavailable holds and the
    call spends no budget (the runtime parses them the same way)."""
    from alloy.limits import parse_retry_at

    now = datetime(2026, 10, 7, 1, 0)
    for error in (_codex_limit_error(), _failure_message("", CURSOR_STDERR, 1)):
        result = AgentResult(
            runner="codex",
            ok=False,
            exit_code=1,
            started_at=now.replace(tzinfo=timezone.utc),
            ended_at=now.replace(tzinfo=timezone.utc),
            duration_s=2.0,
            error=error,
            retry_at=parse_retry_at(error, now=now),
        )
        assert is_unavailable(result)


def test_recipe_codex_tier_entries_time_out_after_15_minutes_with_a_fallback():
    from alloy.config import load_recipe

    recipe = load_recipe("tdd-loop-sol-no-context")
    for level, chain in recipe.complexity.tiers.items():
        spec = chain
        while spec is not None:
            if spec.runner == "codex":
                assert spec.timeout_minutes == 15, (level, spec.label)
                assert spec.fallback is not None, (level, spec.label)
            spec = spec.fallback
    implement = recipe.roles["implement"]
    assert implement.runner == "astra" and implement.timeout_minutes == 15
    assert recipe.roles["judge"].timeout_minutes == 10  # other roles unchanged
    assert recipe.roles["verifier"].timeout_minutes == 10


def test_status_json_lists_runners_unavailable(beads_project, alloy_home):
    import json

    from typer.testing import CliRunner

    from alloy.cli import app
    from alloy.engine import Engine

    engine = Engine.open(beads_project, alloy_home)
    until = utcnow() + timedelta(hours=2)
    engine.store.mark_runner_unavailable("codex", until, reason="You've hit your usage limit", parsed=True)
    engine.store.mark_runner_unavailable("cursor", until, model="kimi-k3-high", reason="usage limit")
    engine.store.mark_runner_unavailable("claude", utcnow() - timedelta(minutes=1), reason="expired")

    result = CliRunner().invoke(app, ["status", "--repo", str(beads_project), "--root", str(alloy_home), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    entries = payload["runners_unavailable"]
    assert [(e["harness"], e["model"]) for e in entries] == [("codex", None), ("cursor", "kimi-k3-high")]
    assert datetime.fromisoformat(entries[0]["until"]) == until


async def test_a_dated_codex_reset_days_ahead_round_trips_through_runner_breaker(store, tmp_path):
    # The real 2026-10-08 wording (typographic apostrophe, ordinal day, year),
    # dated relative to the wall clock so the test does not rot.
    reset = (datetime.now() + timedelta(days=6)).replace(hour=5, minute=31, second=0, microsecond=0)
    error = (
        "You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
        "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at "
        f"{reset:%b} {reset.day}th, {reset.year} 5:31 AM."
    )
    codex = ScriptedRunner("codex", (False, error, 1.4))
    claude = ScriptedRunner("claude-write")
    ctx = _ctx(store, tmp_path, {"codex": codex, "claude-write": claude})

    await ctx.call("implement", CHAIN, "prompt")

    (entry,) = Store(store.path).runners_unavailable()
    assert (entry["harness"], entry["model"], entry["reset_parsed"]) == ("codex", None, True)
    until = datetime.fromisoformat(entry["until"])
    assert until.tzinfo is not None
    assert until == reset.astimezone()  # honoured days ahead, not clamped to hours
    assert store.runner_unavailable_until("codex", "gpt-6.1-sol") == until
    with store.connect() as conn:
        assert conn.execute("SELECT parsed FROM runner_breaker WHERE harness = 'codex'").fetchone()[0] == 1
