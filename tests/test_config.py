"""Coverage for the recipe `complexity:` block (alloy-0uc.1).

`complexity:` adds tier fallback chains (simple/medium/complex) and a
routing knob (shadow/live) on top of the existing per-role `RoleSpec`
machinery. In shadow mode nothing about dispatch changes -- a `tiered: true`
role still uses its own `runner:`/`fallback:` -- so most of this file is
pure parsing: does `RecipeConfig.parse` build the right `ComplexitySpec`,
does `resolve_role` pick the tier chain only when routing is live and the
complexity level is known, and do bad inputs raise `ConfigError` rather than
silently doing something else.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from alloy.config import ConfigError, RecipeConfig, RoleSpec, load_recipe
from alloy.models import COMPLEXITY_LEVELS, next_level


# ---------------------------------------------------------------------------
# models.py: complexity level primitives
# ---------------------------------------------------------------------------


def test_complexity_levels_are_simple_medium_complex():
    assert COMPLEXITY_LEVELS == ("simple", "medium", "complex")


def test_next_level_escalates_one_step():
    assert next_level("simple") == "medium"
    assert next_level("medium") == "complex"


def test_next_level_complex_stays_complex():
    assert next_level("complex") == "complex"


# ---------------------------------------------------------------------------
# Built-in recipes: tdd-loop and tdd-loop-jev
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recipe_name", ["tdd-loop", "tdd-loop-jev"])
def test_builtin_recipe_complexity_defaults(recipe_name):
    config = load_recipe(recipe_name)
    assert config.complexity.routing == "live"
    assert config.complexity.escalate_after_retries == 2


@pytest.mark.parametrize("recipe_name", ["tdd-loop", "tdd-loop-jev"])
def test_builtin_recipe_simple_tier_chain(recipe_name):
    config = load_recipe(recipe_name)
    simple = config.complexity.tiers["simple"]
    assert simple.runner == "cursor"
    assert simple.model == "composer-2.5"
    assert simple.effort is None

    fb1 = simple.fallback
    assert fb1 is not None
    assert fb1.runner == "codex"
    assert fb1.model == "gpt-6-luna"
    assert fb1.effort is None

    fb2 = fb1.fallback
    assert fb2 is not None
    assert fb2.runner == "claude-write"
    assert fb2.model == "haiku"
    assert fb2.effort == "low"
    assert fb2.fallback is None


@pytest.mark.parametrize("recipe_name", ["tdd-loop", "tdd-loop-jev"])
def test_builtin_recipe_medium_tier_chain(recipe_name):
    config = load_recipe(recipe_name)
    medium = config.complexity.tiers["medium"]
    assert medium.runner == "cursor"
    assert medium.model == "composer-2.5"
    assert medium.effort is None

    fb1 = medium.fallback
    assert fb1 is not None
    assert fb1.runner == "codex"
    assert fb1.model == "gpt-6-sol"
    assert fb1.effort == "high"

    fb2 = fb1.fallback
    assert fb2 is not None
    assert fb2.runner == "claude-write"
    assert fb2.model == "sonnet"
    assert fb2.effort == "high"
    assert fb2.fallback is None


@pytest.mark.parametrize("recipe_name", ["tdd-loop", "tdd-loop-jev"])
def test_builtin_recipe_estimate_role_uses_jev_with_cursor_fallback(recipe_name):
    config = load_recipe(recipe_name)
    estimate = config.roles["estimate"]
    assert estimate.runner == "jev"
    assert estimate.model == "jev-latest"
    assert estimate.fallback is not None
    assert estimate.fallback.runner == "cursor"
    assert estimate.fallback.model == "composer-2.5"


@pytest.mark.parametrize("recipe_name", ["tdd-loop", "tdd-loop-jev"])
def test_builtin_recipe_complex_tier_chain(recipe_name):
    config = load_recipe(recipe_name)
    complex_ = config.complexity.tiers["complex"]
    assert complex_.runner == "cursor"
    assert complex_.model == "kimi-k3-high"
    assert complex_.effort is None

    fb1 = complex_.fallback
    assert fb1 is not None
    assert fb1.runner == "claude-write"
    assert fb1.model == "opus"
    assert fb1.effort is None

    fb2 = fb1.fallback
    assert fb2 is not None
    assert fb2.runner == "astra"
    assert fb2.model is None
    assert fb2.effort is None
    assert fb2.fallback is None


@pytest.mark.parametrize("recipe_name", ["tdd-loop", "tdd-loop-jev"])
def test_builtin_recipe_implement_role_is_tiered_and_unchanged(recipe_name):
    config = load_recipe(recipe_name)
    implement = config.roles["implement"]
    assert implement.tiered is True
    assert implement.runner == "astra"
    assert implement.fallback is not None
    assert implement.fallback.runner == "claude-write"


@pytest.mark.parametrize("recipe_name", ["tdd-loop", "tdd-loop-jev"])
def test_builtin_recipe_tests_role_is_pinned_not_tiered(recipe_name):
    config = load_recipe(recipe_name)
    tests_role = config.roles["tests"]
    assert tests_role.tiered is False
    assert tests_role.runner == "cursor"
    assert tests_role.model == "composer-2.5"
    assert tests_role.fallback is not None
    assert tests_role.fallback.runner == "claude-write"
    assert tests_role.fallback.model == "sonnet"


@pytest.mark.parametrize("recipe_name", ["tdd-loop", "tdd-loop-jev"])
def test_builtin_recipe_scope_role_uses_jev_with_cursor_fallback(recipe_name):
    config = load_recipe(recipe_name)
    scope = config.roles["scope"]
    assert scope.runner == "jev"
    assert scope.model == "jev-latest"
    assert scope.fallback is not None
    assert scope.fallback.runner == "cursor"
    assert scope.fallback.model == "composer-2.5"


@pytest.mark.parametrize("recipe_name", ["tdd-loop", "tdd-loop-jev"])
def test_builtin_recipe_context_and_judge_unchanged(recipe_name):
    config = load_recipe(recipe_name)
    context = config.roles["context"]
    assert context.runner == "cursor-plan"
    assert context.timeout_minutes == 10

    judge = config.roles["judge"]
    if recipe_name == "tdd-loop":
        assert judge.runner == "claude"
        assert judge.model == "sonnet"
    else:
        assert judge.runner == "jev"
        assert judge.model == "jev-latest"
        assert judge.fallback is not None
        assert judge.fallback.runner == "claude"
        assert judge.fallback.model == "sonnet"


# ---------------------------------------------------------------------------
# resolve_role: shadow vs live routing
# ---------------------------------------------------------------------------


def test_resolve_role_shadow_routing_ignores_tiers():
    config = replace(
        load_recipe("tdd-loop"),
        complexity=replace(load_recipe("tdd-loop").complexity, routing="shadow"),
    )
    assert config.complexity.routing == "shadow"
    resolved = config.resolve_role("implement", "simple")
    assert resolved == config.roles["implement"]
    assert resolved.runner == "astra"


def test_resolve_role_live_routing_uses_tier_chain_for_tiered_role():
    config = load_recipe("tdd-loop")
    live_config = replace(config, complexity=replace(config.complexity, routing="live"))
    resolved = live_config.resolve_role("implement", "simple")
    assert resolved == live_config.complexity.tiers["simple"]
    assert resolved.runner == "cursor"
    assert resolved.model == "composer-2.5"


def test_resolve_role_live_routing_non_tiered_role_uses_own_spec():
    config = load_recipe("tdd-loop")
    live_config = replace(config, complexity=replace(config.complexity, routing="live"))
    resolved = live_config.resolve_role("judge", "simple")
    assert resolved == live_config.roles["judge"]


def test_resolve_role_live_routing_unknown_level_raises():
    config = load_recipe("tdd-loop")
    live_config = replace(config, complexity=replace(config.complexity, routing="live"))
    with pytest.raises(ConfigError):
        live_config.resolve_role("implement", "huge")


def test_resolve_role_live_routing_none_level_raises():
    config = load_recipe("tdd-loop")
    live_config = replace(config, complexity=replace(config.complexity, routing="live"))
    with pytest.raises(ConfigError):
        live_config.resolve_role("implement", None)


# ---------------------------------------------------------------------------
# ComplexitySpec / RecipeConfig.parse validation
# ---------------------------------------------------------------------------


def _base_raw_recipe(**overrides):
    raw = {
        "name": "unit-test-recipe",
        "roles": {
            "context": {"runner": "cursor-plan"},
            "tests": {"runner": "claude-write", "model": "sonnet"},
            "implement": {"runner": "astra"},
            "judge": {"runner": "claude", "model": "sonnet"},
        },
    }
    raw.update(overrides)
    return raw


def test_parse_rejects_invalid_routing_value():
    raw = _base_raw_recipe(complexity={"routing": "sometimes"})
    with pytest.raises(ConfigError):
        RecipeConfig.parse(raw)


def test_parse_rejects_tiered_role_without_complexity_block():
    raw = _base_raw_recipe()
    raw["roles"]["implement"]["tiered"] = True
    with pytest.raises(ConfigError):
        RecipeConfig.parse(raw)


def test_parse_without_complexity_block_defaults_to_empty_shadow():
    raw = _base_raw_recipe()
    config = RecipeConfig.parse(raw)
    assert config.complexity.tiers == {}
    assert config.complexity.routing == "shadow"


def test_parse_rejects_unknown_tier_key():
    raw = _base_raw_recipe(
        complexity={
            "routing": "shadow",
            "tiers": {
                "huge": [{"runner": "cursor", "model": "composer-2.5"}],
            },
        }
    )
    with pytest.raises(ConfigError):
        RecipeConfig.parse(raw)


def test_parse_folds_tier_list_into_fallback_chain():
    raw = _base_raw_recipe(
        complexity={
            "routing": "live",
            "tiers": {
                "simple": [
                    {"runner": "cursor", "model": "composer-2.5"},
                    {"runner": "codex", "model": "gpt-6-luna"},
                ],
            },
        }
    )
    raw["roles"]["implement"]["tiered"] = True
    config = RecipeConfig.parse(raw)
    simple = config.complexity.tiers["simple"]
    assert simple == RoleSpec(
        runner="cursor",
        model="composer-2.5",
        fallback=RoleSpec(runner="codex", model="gpt-6-luna"),
    )


# ---------------------------------------------------------------------------
# RoleSpec.effort (alloy-0uc.14)
# ---------------------------------------------------------------------------


def test_role_spec_parse_effort_sets_field_and_label():
    spec = RoleSpec.parse(
        {"runner": "claude-write", "model": "haiku", "effort": "low"}
    )
    assert spec.effort == "low"
    assert spec.label == "claude-write:haiku@low"


def test_role_spec_parse_codex_without_effort():
    spec = RoleSpec.parse({"runner": "codex"})
    assert spec.effort is None


def test_role_spec_parse_rejects_unknown_effort():
    with pytest.raises(ConfigError):
        RoleSpec.parse({"runner": "claude-write", "effort": "turbo"})


def test_complexity_tier_rejects_effort_on_cursor_runner():
    raw = _base_raw_recipe(
        complexity={
            "routing": "live",
            "tiers": {
                "simple": [
                    {"runner": "cursor", "model": "composer-2.5", "effort": "low"},
                ],
            },
        }
    )
    with pytest.raises(ConfigError):
        RecipeConfig.parse(raw)


def test_complexity_tier_rejects_effort_on_cursor_plan_runner():
    raw = _base_raw_recipe(
        complexity={
            "routing": "live",
            "tiers": {
                "simple": [
                    {"runner": "cursor-plan", "model": "composer-2.5", "effort": "high"},
                ],
            },
        }
    )
    with pytest.raises(ConfigError):
        RecipeConfig.parse(raw)


# ---------------------------------------------------------------------------
# VerificationSpec / verification: block (alloy-21u.1)
# ---------------------------------------------------------------------------


def test_parse_verification_block_overrides_single_field():
    config = RecipeConfig.parse(_base_raw_recipe(verification={"max_total_checks": 7}))
    verification = config.verification
    assert verification.max_total_checks == 7
    assert verification.max_checks_per_iteration == 5
    assert verification.max_command_timeout_minutes == 15.0
    assert verification.max_baseline_repairs == 2
    assert verification.min_acceptance_confidence == 0.6


def test_parse_legacy_verify_timeout_maps_to_verification():
    config = RecipeConfig.parse(_base_raw_recipe(verify={"timeout_minutes": 3}))
    assert config.verification.max_command_timeout_minutes == 3.0


def test_parse_legacy_verify_command_emits_deprecated_warning(caplog):
    raw = _base_raw_recipe(verify={"command": "make test"})
    with caplog.at_level(logging.WARNING, logger="alloy.config"):
        RecipeConfig.parse(raw)
    assert any("deprecated" in record.message.lower() for record in caplog.records)


@pytest.mark.parametrize("recipe_name", ["tdd-loop", "tdd-loop-jev"])
def test_builtin_recipe_verification_defaults(recipe_name):
    from alloy.config import VerificationSpec

    config = load_recipe(recipe_name)
    assert config.verification == VerificationSpec()


# ---------------------------------------------------------------------------
# MemorySpec / memory: block (alloy-4ef.1)
# ---------------------------------------------------------------------------


def test_parse_without_memory_block_yields_memory_spec_defaults():
    from alloy.config import MemorySpec

    config = RecipeConfig.parse(_base_raw_recipe())
    assert config.memory == MemorySpec(
        enabled=True,
        max_items=12,
        max_chars=4000,
        ttl_days=90,
        harvest_min_confidence=0.7,
        review_every_days=7,
        review_every_runs=20,
        instruction_files=["AGENTS.md", "CLAUDE.md"],
    )


def test_parse_memory_block_overrides_single_field():
    from alloy.config import MemorySpec

    config = RecipeConfig.parse(_base_raw_recipe(memory={"max_items": 3}))
    assert config.memory == MemorySpec(
        enabled=True,
        max_items=3,
        max_chars=4000,
        ttl_days=90,
        harvest_min_confidence=0.7,
        review_every_days=7,
        review_every_runs=20,
        instruction_files=["AGENTS.md", "CLAUDE.md"],
    )


def test_parse_rejects_unknown_memory_key():
    with pytest.raises(ConfigError):
        RecipeConfig.parse(_base_raw_recipe(memory={"bogus": 1}))


@pytest.mark.parametrize("recipe_name", ["tdd-loop", "tdd-loop-jev"])
def test_builtin_recipe_memory_defaults(recipe_name):
    from alloy.config import MemorySpec

    config = load_recipe(recipe_name)
    assert config.memory == MemorySpec()


@pytest.mark.parametrize("recipe_name", ["tdd-loop", "tdd-loop-jev"])
def test_builtin_recipe_harvest_role(recipe_name):
    config = load_recipe(recipe_name)
    harvest = config.role("harvest")
    assert harvest.runner == "claude"
    assert harvest.model == "sonnet"


@pytest.mark.parametrize("recipe_name", ["tdd-loop", "tdd-loop-jev"])
def test_builtin_recipe_memory_reviewer_role(recipe_name):
    config = load_recipe(recipe_name)
    reviewer = config.role("memory_reviewer")
    assert reviewer.runner == "cursor"
    assert reviewer.model == "composer-2.5"


# ---------------------------------------------------------------------------
# LandingSpec / landing: block (alloy-vrh.1)
# ---------------------------------------------------------------------------


def test_load_recipe_tdd_loop_landing_mode_auto_target_main():
    config = load_recipe("tdd-loop")
    assert config.landing.mode == "auto"
    assert config.landing.target == "main"


def test_parse_without_landing_block_yields_landing_spec_defaults():
    from alloy.config import LandingSpec

    config = RecipeConfig.parse(_base_raw_recipe())
    assert config.landing == LandingSpec(mode="off", target="main")


def test_parse_landing_block_overrides_mode_and_target():
    from alloy.config import LandingSpec

    config = RecipeConfig.parse(
        _base_raw_recipe(landing={"mode": "auto", "target": "develop"})
    )
    assert config.landing == LandingSpec(mode="auto", target="develop")


def test_parse_rejects_invalid_landing_mode():
    with pytest.raises(ConfigError, match="landing.mode"):
        RecipeConfig.parse(_base_raw_recipe(landing={"mode": "sometimes"}))


def test_parse_landing_mode_yaml_off_boolean_coerces_to_off():
    from alloy.config import LandingSpec

    config = RecipeConfig.parse(_base_raw_recipe(landing={"mode": False}))
    assert config.landing == LandingSpec(mode="off", target="main")


def test_parse_rejects_unknown_landing_key():
    with pytest.raises(ConfigError):
        RecipeConfig.parse(_base_raw_recipe(landing={"bogus": 1}))


@pytest.mark.parametrize("recipe_name", ["tdd-loop", "tdd-loop-jev"])
def test_builtin_recipe_landing_mode_auto(recipe_name):
    from alloy.config import LandingSpec

    config = load_recipe(recipe_name)
    assert config.landing == LandingSpec(mode="auto", target="main")
