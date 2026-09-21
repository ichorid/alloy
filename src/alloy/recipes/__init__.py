"""Recipe registry.

A recipe is a pair of plain Python callables: one that compiles a LangGraph for a
bound :class:`~alloy.runtime.RunContext`, and one that produces its initial state.
Adding a recipe means adding a module and a YAML file -- no DSL, no plugin loader.
"""

from __future__ import annotations

from typing import Any, Callable, NamedTuple

from alloy.recipes import tdd_loop


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
}


def get(name: str) -> Recipe:
    try:
        return REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(REGISTRY))
        raise KeyError(f"unknown recipe graph '{name}'; registered: {known}") from exc


def names() -> list[str]:
    return sorted(REGISTRY)
