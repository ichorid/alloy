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

`prefix_hash` is the sha256 of the first three layers -- the text that must be
byte-identical across every call of a role within a run, and across runs on
the same repository. It is recorded next to the whole-prompt hash on every
agent call so `alloy logs` can show whether the cacheable prefix held still.
"""

from __future__ import annotations

import hashlib
from typing import NamedTuple

LAYER_SEPARATOR = "\n\n---\n\n"


class Prompt(str):
    """The assembled prompt text, with its `prefix_hash` riding along.

    It is a plain `str` in every other respect; the runner reads the attribute
    off the prompt it is given so no call signature has to carry the hash.
    """

    prefix_hash: str

    def __new__(cls, text: str, prefix_hash: str = "") -> "Prompt":
        prompt = super().__new__(cls, text)
        prompt.prefix_hash = prefix_hash
        return prompt


class PromptParts(NamedTuple):
    text: Prompt
    prefix_hash: str


def prefix_hash(static: str, project: str, run: str) -> str:
    """sha256 over the non-empty static, project and run layers, joined as they
    appear at the head of the assembled prompt."""
    return hashlib.sha256(_join(static, project, run).encode("utf-8")).hexdigest()


def assemble(static: str, project: str, run: str, task: str, volatile: str) -> PromptParts:
    """Join the non-empty layers in fixed order with LAYER_SEPARATOR and return
    the text together with the prefix hash of the static, project and run layers."""
    digest = prefix_hash(static, project, run)
    return PromptParts(Prompt(_join(static, project, run, task, volatile), digest), digest)


def _join(*layers: str) -> str:
    return LAYER_SEPARATOR.join(layer for layer in layers if layer)
