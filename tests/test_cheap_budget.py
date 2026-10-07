"""Cheap-call budget: verifier/acceptance/judge/harvest/triage/estimate calls
spend `limits.max_cheap_agent_calls` only, everything else `max_agent_calls`
only (a long verify loop parked green beads on max_agent_calls overnight)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from support import load_config, make_bead
from test_runner_breaker import ScriptedRunner, _Registry

from alloy.config import (
    DEFAULT_CHEAP_ROLES,
    ConfigError,
    Limits,
    RoleSpec,
    load_recipe,
    resolve_max_cheap_agent_calls,
)
from alloy.runtime import RunContext
from alloy.store import Store


def test_defaults_keep_existing_recipes_working():
    limits = Limits.parse({"max_agent_calls": 7})
    assert limits.max_agent_calls == 7
    assert limits.max_cheap_agent_calls == 40
    assert limits.max_cheap_agent_calls_by_tier == {}
    assert limits.cheap_roles == DEFAULT_CHEAP_ROLES
    assert set(DEFAULT_CHEAP_ROLES) == {"verifier", "acceptance", "harvest", "triage", "estimate", "judge"}
    for name in ("tdd-loop", "tdd-loop-sonnet", "fast-track", "tdd-loop-jev"):
        load_recipe(name)  # still parses


def test_parse_cheap_limits_and_tier_map():
    limits = Limits.parse(
        {
            "max_cheap_agent_calls": 80,
            "max_cheap_agent_calls_by_tier": {"complex": 120},
            "cheap_roles": ["verifier", "judge"],
        }
    )
    assert limits.max_cheap_agent_calls == 80
    assert limits.max_cheap_agent_calls_by_tier == {"complex": 120}
    assert limits.cheap_roles == ("verifier", "judge")
    assert limits.is_cheap_role("judge")
    assert not limits.is_cheap_role("implement")
    with pytest.raises(ConfigError):
        Limits.parse({"max_cheap_agent_calls_by_tier": {"huge": 1}})
    with pytest.raises(ConfigError):
        Limits.parse({"cheap_roles": "verifier"})


def test_sol_recipe_sets_the_cheap_budget():
    recipe = load_recipe("tdd-loop-sol-no-context")
    assert recipe.limits.max_cheap_agent_calls == 80
    assert resolve_max_cheap_agent_calls(recipe, "simple") == 80
    assert resolve_max_cheap_agent_calls(recipe, "complex") == 120
    assert resolve_max_cheap_agent_calls(recipe, None) == 80
    assert recipe.limits.max_agent_calls_by_tier["complex"] == 64


@pytest.fixture
def make_ctx(tmp_path):
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

    def make(runners, **limits):
        config = load_config()
        recipe = replace(config, limits=replace(config.limits, **limits))
        return RunContext(
            bead=make_bead(),
            recipe=recipe,
            run_id="run-1",
            worktree=SimpleNamespace(path=tmp_path),
            worktrees=None,
            registry=_Registry(runners),
            store=store,
            checkpointer=None,
            log_dir=tmp_path,
        )

    return make


async def test_cheap_roles_spend_only_the_cheap_budget(make_ctx):
    runner = ScriptedRunner("claude")
    ctx = make_ctx({"claude": runner}, max_agent_calls=2, max_cheap_agent_calls=3)
    spec = RoleSpec(runner="claude")

    for role in ("verifier", "acceptance", "critic:claude"):
        await ctx.call(role, spec, "prompt")
    await ctx.call("implement", spec, "prompt")

    assert ctx.store.call_count("run-1") == 2  # critic + implement
    assert ctx.store.call_count("run-1", cheap=True) == 2
    record = ctx.store.get_run("run-1")
    assert (record["agent_calls"], record["cheap_agent_calls"]) == (2, 2)
    assert ctx.check_limits({}) == "max_agent_calls reached (2/2)"


async def test_check_limits_reports_the_cheap_budget(make_ctx):
    runner = ScriptedRunner("claude")
    ctx = make_ctx({"claude": runner}, max_agent_calls=10, max_cheap_agent_calls=2)
    spec = RoleSpec(runner="claude")
    await ctx.call("judge", spec, "prompt")
    assert ctx.check_limits({}) is None
    await ctx.call("verifier", spec, "prompt")
    assert ctx.check_limits({}) == "max_cheap_agent_calls reached (2/2)"
    # A human resume grants another window, as for every other budget.
    assert ctx.check_limits({"budget_extensions": 1}) is None
    assert "2/4 cheap-role calls used" in ctx.limits_note({"budget_extensions": 1})


async def test_chain_and_unavailable_rules_apply_to_the_cheap_budget(make_ctx):
    codex = ScriptedRunner("codex", (False, "boom", 1.0))
    limited = ScriptedRunner("cursor", (False, "You've hit your usage limit", 1.0))
    claude = ScriptedRunner("claude")
    ctx = make_ctx({"codex": codex, "cursor": limited, "claude": claude})

    chain = RoleSpec.parse({"runner": "codex", "fallback": {"runner": "claude"}})
    await ctx.call("judge", chain, "prompt")  # failed primary + fallback = one call
    assert ctx.store.call_count("run-1", cheap=True) == 1

    await ctx.call("verifier", RoleSpec(runner="cursor"), "prompt")  # usage limit: free
    assert ctx.store.call_count("run-1", cheap=True) == 1
    assert ctx.store.call_count("run-1") == 0


async def test_tier_scaled_cheap_ceiling(make_ctx):
    ctx = make_ctx({}, max_cheap_agent_calls=5, max_cheap_agent_calls_by_tier={"complex": 9})
    assert ctx.cheap_call_limit({"complexity": "complex"}) == 9
    assert ctx.cheap_call_limit({"complexity": "simple"}) == 5
