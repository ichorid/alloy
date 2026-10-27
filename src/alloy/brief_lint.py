"""Bead brief lint: what agents see of a bead, minus git-write instructions.

Alloy commits a finished bead itself, and a sandboxed agent's `.git` is
read-only: a bead description saying "Commit: fix(client): ..." or "push to
main" made agents try anyway and park the run. Such lines are dropped from
the bead the run's agents see (`Engine.build_context`); the bd text is never
modified, and one `brief-lint` event per run names what was dropped.

Dropping is deliberately conservative: whole lines only, outside fenced code
blocks, and only when the line *starts* with the instruction (after a bullet,
a number, or a "then"/"and"/"finally"/"please"). A line that merely mentions
commits -- "the commit message includes the bead id", "do not run git push"
-- is kept.

In a ship context (a version bump), hard-coded version numbers are flagged in
the event as a warning, never dropped: the bump script decides the version.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from alloy.beads import Bead

_LEAD = (
    r"^\s*(?:[-*+>]\s*|\d+[.)]\s*)?(?:\*\*)?\s*"
    r"(?:(?:then|and|finally|please|also|next|afterwards?|lastly)[,:]?\s+)*"
)
_CONVENTIONAL_TYPES = r"(?:feat|fix|chore|docs|refactor|test|tests|perf|build|ci|style|revert|release)"
_THE_WORK = (
    r"(?:it|this|them|everything|all(?:\s+(?:the\s+)?changes)?|(?:the|your)\s+(?:change|changes|work|fix|result))"
)

GIT_WRITE_LINE_PATTERNS: dict[str, re.Pattern[str]] = {
    # "Commit: fix(client): ...", "**Commit:** `feat: ...`", "Commit message: chore(x): ..."
    "conventional-commit": re.compile(
        _LEAD
        + r"commit(?:\s+message)?\s*(?:\*\*)?\s*:\s*(?:\*\*)?\s*[`'\"]?"
        + _CONVENTIONAL_TYPES
        + r"(?:\([^)]*\))?!?:",
        re.IGNORECASE,
    ),
    # "git commit -am ...", "Run `git push origin main`", "then git tag v1"
    "git-write": re.compile(
        _LEAD + r"(?:run\s+)?[`'\"]?git\s+(?:commit|push|tag|merge|rebase|reset|cherry-pick|am|revert|stash)\b",
        re.IGNORECASE,
    ),
    # "Push to main", "Commit and push to main", "push it to origin"
    "push-to-remote": re.compile(
        _LEAD + r"(?:commit(?:\s+" + _THE_WORK + r")?\s+and\s+)?push(?:\s+" + _THE_WORK + r")?\s+to\s+"
        r"(?:origin|main|master|(?:the\s+)?remote)\b",
        re.IGNORECASE,
    ),
    # "Commit the changes.", "3. Commit your work"
    "commit-the-work": re.compile(_LEAD + r"commit\s+" + _THE_WORK + r"\b", re.IGNORECASE),
}
"""Every pattern a whole brief line is dropped on. Each is anchored at the
start of the line, so prose that mentions commits is never matched."""

VERSION_PIN = re.compile(r"(?<![\w.])v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?(?![\w.])")

_FENCE = re.compile(r"^\s*(```|~~~)")


class LintResult(NamedTuple):
    bead: Bead
    dropped: list[str]
    version_pins: list[str]


def lint_text(text: str) -> tuple[str, list[str]]:
    """`text` without its git-write instruction lines, and those lines."""
    kept: list[str] = []
    dropped: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = _FENCE.match(line)
        if marker:
            fence = None if fence == marker.group(1) else (fence or marker.group(1))
        elif fence is None and any(pattern.match(line) for pattern in GIT_WRITE_LINE_PATTERNS.values()):
            dropped.append(line.strip())
            continue
        kept.append(line)
    if not dropped:
        return text, []
    return "\n".join(kept), dropped


def lint_bead(bead: Bead, *, ship_context: bool) -> LintResult:
    """The bead as agents should see it: description, design and acceptance
    criteria linted. `bead` itself is never modified."""
    fields = ("description", "design", "acceptance_criteria")
    update: dict[str, str] = {}
    dropped: list[str] = []
    for name in fields:
        text, lines = lint_text(getattr(bead, name))
        if lines:
            update[name] = text
            dropped.extend(lines)
    pins: list[str] = []
    if ship_context:
        texts = [update.get(name, getattr(bead, name)) for name in fields]
        pins = list(dict.fromkeys(match.group(0) for text in texts for match in VERSION_PIN.finditer(text)))
    return LintResult(bead.model_copy(update=update) if update else bead, dropped, pins)


_SHIP_WORDS = re.compile(r"\b(?:ship|release|version\s+bump|bump\w*\s+(?:the\s+)?(?:\w+\s+)?version)\b", re.I)


def is_ship_context(bead: Bead, *, recipe_name: str, graph: str | None) -> bool:
    """A version-bump/ship bead: a `script` (or `ship`) recipe, or a title or
    label that says ship, release or version bump."""
    if graph == "script" or "ship" in recipe_name:
        return True
    return bool(_SHIP_WORDS.search(bead.title) or any(_SHIP_WORDS.search(label) for label in bead.labels))
