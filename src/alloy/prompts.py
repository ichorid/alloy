"""Prompt assembly: five ordered layers joined by one fixed separator.

Every role prompt is built from the same layers, in this order, so that the
stable text comes first and the volatile text last:

- static:   role instructions, rules and the bug protocol -- identical for every
            bead, run and iteration of a role
- project:  rendered project memory -- identical for every bead of a project
- run:      repository context and check hints -- identical for one run
- task:     brief, acceptance criteria and baseline checks -- identical for
            every iteration and check of one run
- volatile: check results, history, diff, iteration instructions, budget and
            resumed-session continuation text

Empty layers are skipped. Prompts of one role therefore share a common prefix
up to the first layer that differs, which is what prompt caching rewards.
"""

from __future__ import annotations

LAYER_SEPARATOR = "\n\n---\n\n"


def assemble(static: str, project: str, run: str, task: str, volatile: str) -> str:
    """Join the non-empty layers in fixed order with LAYER_SEPARATOR."""
    return LAYER_SEPARATOR.join(
        layer for layer in (static, project, run, task, volatile) if layer
    )
