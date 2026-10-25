"""A call stopped at its role's time limit continues its own session.

The resumed session keeps its prompt cache warm and knows what it already
did, so Alloy resumes it (`limits.timeout_resumes`, 1 by default) before the
chain falls back to a cold harness. Only a time-limit stop resumes: a failed
harness, or one that hit a usage or token limit, goes straight to the
fallback, so a session that ran out of context can never loop.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from support import make_bead

from alloy.config import Limits, RoleSpec
from alloy.runners import RunnerRegistry
from alloy.runtime import RunContext
from alloy.store import Store

FAST_TIMEOUT = 2 / 60  # 2 s, in minutes


def _ctx(project, tmp_path, *, timeout_resumes: int = 1) -> RunContext:
    store = Store(tmp_path / "alloy.db")
    store.create_run(
        run_id="run-timeout",
        bead_id="t-1",
        thread_id="run-timeout",
        recipe="tdd-loop",
        repo=project,
        worktree=None,
        branch=None,
        log_dir=None,
    )
    return RunContext(
        bead=make_bead(),
        recipe=SimpleNamespace(limits=Limits(timeout_resumes=timeout_resumes)),
        run_id="run-timeout",
        worktree=SimpleNamespace(path=project),
        worktrees=None,
        registry=RunnerRegistry(log_dir=tmp_path / "logs"),
        store=store,
        checkpointer=None,
        log_dir=tmp_path,
        beads=None,
    )


def _chain() -> RoleSpec:
    return RoleSpec.parse(
        {
            "runner": "codex",
            "timeout_minutes": FAST_TIMEOUT,
            "fallback": {"runner": "claude-write", "model": "sonnet", "timeout_minutes": FAST_TIMEOUT},
        }
    )


async def test_a_timed_out_codex_call_resumes_its_own_session(fake_harnesses, project, tmp_path):
    fake_harnesses.configure(
        {
            "implement": {"sleep": 10, "session_id": "thread-1"},
            "timeout_resume": {"text": "finished", "session_id": "thread-1"},
        }
    )
    ctx = _ctx(project, tmp_path)

    result = await ctx.call("implement", _chain(), "Implement the smallest change.")

    assert result.ok and result.text == "finished"
    resumed = fake_harnesses.calls_for("timeout_resume")
    assert [(c["runner"], c.get("resume")) for c in resumed] == [("codex", "thread-1")]
    assert not [c for c in fake_harnesses.calls if c["runner"].startswith("claude")]


async def test_a_second_timeout_falls_back_cold(fake_harnesses, project, tmp_path):
    fake_harnesses.configure(
        {
            "implement@codex": {"sleep": 10, "session_id": "thread-1"},
            "implement@claude": {"text": "claude did it"},
            "timeout_resume": {"sleep": 10, "session_id": "thread-1"},
        }
    )
    ctx = _ctx(project, tmp_path)

    result = await ctx.call("implement", _chain(), "Implement the smallest change.")

    assert result.ok and result.runner == "claude-write" and result.text == "claude did it"
    assert len(fake_harnesses.calls_for("timeout_resume")) == 1
    fallback = [c for c in fake_harnesses.calls if c["runner"] == "claude"]
    assert len(fallback) == 1 and "resume" not in fallback[0]


async def test_a_failed_call_is_never_resumed(fake_harnesses, project, tmp_path):
    fake_harnesses.configure(
        {
            "implement@codex": {"exit": 1, "stderr": "context window exceeded", "session_id": "thread-1"},
            "implement@claude": {"text": "ok"},
        }
    )
    ctx = _ctx(project, tmp_path)

    result = await ctx.call("implement", _chain(), "Implement the smallest change.")

    assert result.ok and result.runner == "claude-write"
    assert fake_harnesses.calls_for("timeout_resume") == []


async def test_timeout_resumes_zero_falls_back_at_once(fake_harnesses, project, tmp_path):
    fake_harnesses.configure(
        {"implement@codex": {"sleep": 10, "session_id": "thread-1"}, "implement@claude": {"text": "ok"}}
    )
    ctx = _ctx(project, tmp_path, timeout_resumes=0)

    result = await ctx.call("implement", _chain(), "Implement the smallest change.")

    assert result.ok and result.runner == "claude-write"
    assert fake_harnesses.calls_for("timeout_resume") == []


async def test_a_timed_out_claude_call_resumes_the_session_id_it_was_given(fake_harnesses, project, tmp_path):
    fake_harnesses.configure({"implement": {"sleep": 10}, "timeout_resume": {"text": "finished"}})
    ctx = _ctx(project, tmp_path)
    spec = RoleSpec.parse({"runner": "claude-write", "model": "sonnet", "timeout_minutes": FAST_TIMEOUT})

    result = await ctx.call("implement", spec, "Implement the smallest change.")

    assert result.ok
    first = fake_harnesses.calls_for("implement")[0]["argv"]
    assigned = first[first.index("--session-id") + 1]
    resumed = fake_harnesses.calls_for("timeout_resume")
    assert [c.get("resume") for c in resumed] == [assigned]
    assert "--session-id" not in resumed[0]["argv"]


def test_timeout_resumes_parses_and_defaults_to_one():
    assert Limits.parse({}).timeout_resumes == 1
    assert Limits.parse({"timeout_resumes": 0}).timeout_resumes == 0
    assert Limits.parse({"timeout_resumes": -3}).timeout_resumes == 0


async def test_the_ledger_keeps_the_session_id_of_a_timed_out_call(fake_harnesses, project, tmp_path):
    fake_harnesses.configure(
        {"implement": {"sleep": 10, "session_id": "thread-1"}, "timeout_resume": {"exit": 1, "session_id": "thread-1"}}
    )
    ctx = _ctx(project, tmp_path)
    spec = RoleSpec.parse({"runner": "codex", "timeout_minutes": FAST_TIMEOUT})

    await ctx.call("implement", spec, "Implement the smallest change.")

    usages = [json.loads(call["usage_json"] or "{}") for call in ctx.store.agent_calls("run-timeout")]
    assert usages[0].get("timed_out") is True and usages[0].get("session_id") == "thread-1"
