"""Recipe registry.

A recipe is a pair of plain Python callables: one that compiles a LangGraph for a
bound :class:`~alloy.runtime.RunContext`, and one that produces its initial state.
Adding a recipe means adding a module and a YAML file -- no DSL, no plugin loader.
"""

from __future__ import annotations

from functools import partial
from typing import Any, Callable, NamedTuple

from alloy.recipes import fast_track, tdd_loop


class Recipe(NamedTuple):
    name: str
    build_graph: Callable[[Any], Any]
    initial_state: Callable[[Any], dict]
    description: str


REGISTRY: dict[str, Recipe] = {
    "tdd-loop": Recipe(
        name="tdd-loop",
        build_graph=tdd_loop.build_graph,
        initial_state=tdd_loop.initial_state,
        description="Context, failing tests, implement, verify, judge, retry/consilium loop",
    ),
    "tdd-loop-jev": Recipe(
        name="tdd-loop-jev",
        build_graph=tdd_loop.build_graph,
        initial_state=tdd_loop.initial_state,
        description="tdd-loop with Jev (Typesafe AI) as the judge instead of Claude",
    ),
    "tdd-loop-sonnet": Recipe(
        name="tdd-loop-sonnet",
        build_graph=tdd_loop.build_graph,
        initial_state=tdd_loop.initial_state,
        description="tdd-loop with Sonnet, then Codex gpt-6-sol, implementing the medium tier",
    ),
    "tdd-loop-sonnet-no-context": Recipe(
        name="tdd-loop-sonnet-no-context",
        build_graph=partial(tdd_loop.build_graph, skip_context=True),
        initial_state=tdd_loop.initial_state,
        description="tdd-loop-sonnet with the context-gathering phase removed: estimate runs first, straight off START",
    ),
    "tdd-loop-sol-no-context": Recipe(
        name="tdd-loop-sol-no-context",
        build_graph=partial(tdd_loop.build_graph, skip_context=True),
        initial_state=tdd_loop.initial_state,
        description="tdd-loop-sonnet-no-context with every Claude model slot replaced by Codex gpt-6.1-sol",
    ),
    "tdd-loop-medium-no-context": Recipe(
        name="tdd-loop-medium-no-context",
        build_graph=partial(tdd_loop.build_graph, skip_context=True),
        initial_state=tdd_loop.initial_state,
        description="Fixed medium-tier tdd-loop (no live complexity routing): Sonnet primary, Codex gpt-6.1-sol fallback",
    ),
    "tdd-loop-complex-no-context": Recipe(
        name="tdd-loop-complex-no-context",
        build_graph=partial(tdd_loop.build_graph, skip_context=True),
        initial_state=tdd_loop.initial_state,
        description="Fixed complex-tier tdd-loop (no live complexity routing): Sonnet primary, Codex gpt-6.1-sol then Cursor kimi-k3-high fallback",
    ),
    "fast-track": Recipe(
        name="fast-track",
        build_graph=fast_track.build_graph,
        initial_state=fast_track.initial_state,
        description="Single-role autonomous loop: one agent implements, proposes and runs its "
        "own real verification commands, and self-judges done/retry -- for simple beads or "
        "beads TDD fits poorly",
    ),
}


def get(name: str) -> Recipe:
    try:
        return REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(REGISTRY))
        raise KeyError(f"unknown recipe graph '{name}'; registered: {known}") from exc


def names() -> list[str]:
    return sorted(REGISTRY)
