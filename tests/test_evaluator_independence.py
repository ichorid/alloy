"""`alloy recipes` warns when an evaluator shares the implementer's model family."""

from __future__ import annotations

from dataclasses import replace

from support import load_config

from alloy.config import RoleSpec
from alloy.recipe_commands import evaluator_warnings, model_family


def test_model_family_reads_the_model_then_the_runner():
    assert model_family(RoleSpec(runner="cursor", model="composer-2.5")) == "cursor"
    assert model_family(RoleSpec(runner="cursor", model="claude-sonnet-5-5")) == "anthropic"
    assert model_family(RoleSpec(runner="claude", model="haiku")) == "anthropic"
    assert model_family(RoleSpec(runner="codex", model=None)) == "openai"
    assert model_family(RoleSpec(runner="jev", model="jev-latest")) is None


def _with(config, **roles):
    return replace(config, roles={**config.roles, **roles})


def test_a_judge_from_the_implementers_family_is_flagged():
    config = _with(
        load_config(),
        implement=RoleSpec(runner="claude-write", model="sonnet"),
        judge=RoleSpec(runner="claude", model="haiku"),
        acceptance=RoleSpec(runner="cursor", model="composer-2.5"),
    )
    warnings = evaluator_warnings(config)
    assert len(warnings) == 1 and warnings[0].startswith("judge (claude/haiku) shares the anthropic")


def test_independent_evaluators_are_quiet():
    config = _with(
        load_config(),
        implement=RoleSpec(runner="cursor", model="composer-2.5"),
        judge=RoleSpec(runner="claude", model="haiku"),
        acceptance=RoleSpec(runner="codex", model="gpt-6-luna"),
    )
    assert evaluator_warnings(config) == []
