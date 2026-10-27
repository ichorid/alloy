"""Bead brief lint: what agents see of a bead, minus git-write instructions.

Alloy commits a finished bead itself, and a sandboxed agent's `.git` is
read-only: a bead description saying "Commit: fix(client): ..." or "push to
main" made agents try anyway and park the run. Such lines are dropped from
the description and design the run's agents see (`Engine.build_context`);
acceptance criteria are behaviour under test and are only reported. The bd
text is never modified, and one `brief-lint` event per run names what was
dropped or looked suspicious.

Dropping is deliberately conservative: never inside fenced code blocks, and
only when the line *starts* with the instruction (after a bullet, a number,
or a "then"/"and"/"finally"/"please") -- the whole line goes. The one
exception is a "Commit: <type>(<scope>): ..." clause after a sentence
boundary mid-line, as bead descriptions usually end a paragraph with it: only
that clause is cut out. A line that merely mentions commits -- "the commit
message includes the bead id", "do not run git push" -- is kept.

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
_VERB_END = r"(?=[\s`'\"]|$)"
_THE_WORK = (
    r"(?:it|this|them|everything|all(?:\s+(?:the\s+)?changes)?|(?:the|your)\s+(?:change|changes|work|fix|result))"
)

GIT_WRITE_LINE_PATTERNS: dict[str, re.Pattern[str]] = {
    # "Commit: fix(client): ...", "**Commit:** `feat: ...`", "Commit message: chore(x): ..." --
    # at the start of a line or after a sentence boundary (`.`/`!`/`?`/`;` +
    # whitespace). Only this clause is dropped (see `COMMIT_CLAUSE_END`), not
    # the rest of its line.
    "conventional-commit": re.compile(
        r"(?:"
        + _LEAD
        + r"|(?<=[.!?;])\s+)(?:\*\*)?\s*commit(?:\s+message)?\s*(?:\*\*)?\s*:\s*(?:\*\*)?\s*[`'\"]?"
        + _CONVENTIONAL_TYPES
        + r"(?:\([^)]*\))?!?:",
        re.IGNORECASE,
    ),
    # "git commit -am ...", "Run `git push origin main`", "then git tag v1" -- the verb
    # must end there (`git merge-base` is not `git merge`), and the read-only
    # forms `git tag -l/--list`, `git stash list/show` never match.
    "git-write": re.compile(
        _LEAD
        + r"(?:run\s+)?[`'\"]?git\s+(?:"
        + r"(?:commit|push|merge|rebase|reset|cherry-pick|am|revert)"
        + _VERB_END
        + r"|tag"
        + _VERB_END
        + r"(?!\s+(?:-l|--list)(?=[\s`'\"]|$))"
        + r"|stash"
        + _VERB_END
        + r"(?!\s+(?:list|show)\b))",
        re.IGNORECASE,
    ),
    # "Push to main", "Commit and push to main", "push it to origin"; "the remote"
    # only as the end of the sentence ("push to remote subscribers" is prose)
    "push-to-remote": re.compile(
        _LEAD + r"(?:commit(?:\s+" + _THE_WORK + r")?\s+and\s+)?push(?:\s+" + _THE_WORK + r")?\s+to\s+"
        r"(?:origin\b|main\b|master\b|(?:the\s+)?remote(?=\s*[.!]?\s*$))",
        re.IGNORECASE,
    ),
    # "Commit the changes.", "3. Commit your work" -- only as the whole line
    "commit-the-work": re.compile(_LEAD + r"commit\s+" + _THE_WORK + r"\s*[.!]?\s*$", re.IGNORECASE),
}
"""Every pattern a brief line is linted on. Each is anchored at the start of
the line (`conventional-commit` also at a sentence boundary), so prose that
mentions commits is never matched. `conventional-commit` drops its clause;
the others drop the whole line."""

COMMIT_CLAUSE_END = re.compile(r"\([Pp]art of #\d+\)\.?|;|\.(?=\s+[A-Z])")
"""Where a commit-instruction clause ends: after a `(Part of #N)` group (and a
following `.`), at a `;` (what follows it stays), at a `.` that ends the
sentence; else at the end of the line."""

VERSION_PIN = re.compile(r"(?<![\w.])v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?(?![\w.])")

_FENCE = re.compile(r"^\s*(```|~~~)")


class LintResult(NamedTuple):
    bead: Bead
    dropped: list[str]
    version_pins: list[str]
    suspicious: list[str] = []
    """Acceptance-criteria lines that look like git-write instructions: only
    reported, never dropped -- acceptance text is behaviour under test."""


def _drop_commit_clauses(line: str, dropped: list[str]) -> str:
    """`line` without its commit-instruction clauses (each one appended to
    `dropped`); "" when nothing but the clause was on it."""
    pattern = GIT_WRITE_LINE_PATTERNS["conventional-commit"]
    while (head := pattern.search(line)) is not None:
        end = COMMIT_CLAUSE_END.search(line, head.end())
        cut = end.end() if end else len(line)
        before, rest = line[: head.start()].rstrip(), line[cut:].strip()
        if not before.strip() and not rest:
            dropped.append(line.strip())
            return ""
        dropped.append(line[head.start() : cut].strip())
        line = f"{before} {rest}" if before.strip() and rest else before or rest
    return line


def lint_text(text: str) -> tuple[str, list[str]]:
    """`text` without its git-write instructions -- commit clauses cut out of
    their line, other instruction lines dropped whole -- and what was dropped."""
    kept: list[str] = []
    dropped: list[str] = []
    fence: str | None = None
    whole_line = [pattern for name, pattern in GIT_WRITE_LINE_PATTERNS.items() if name != "conventional-commit"]
    for line in text.splitlines():
        marker = _FENCE.match(line)
        if marker:
            fence = None if fence == marker.group(1) else (fence or marker.group(1))
        elif fence is None:
            line = _drop_commit_clauses(line, dropped)
            if not line:
                continue
            if any(pattern.match(line) for pattern in whole_line):
                dropped.append(line.strip())
                continue
        kept.append(line)
    if not dropped:
        return text, []
    return "\n".join(kept), dropped


def lint_bead(bead: Bead, *, ship_context: bool) -> LintResult:
    """The bead as agents should see it: description and design linted;
    acceptance criteria only checked (`suspicious`), never changed. `bead`
    itself is never modified."""
    update: dict[str, str] = {}
    dropped: list[str] = []
    for name in ("description", "design"):
        text, lines = lint_text(getattr(bead, name))
        if lines:
            update[name] = text
            dropped.extend(lines)
    _, suspicious = lint_text(bead.acceptance_criteria)
    pins: list[str] = []
    if ship_context:
        texts = [update.get(name, getattr(bead, name)) for name in ("description", "design", "acceptance_criteria")]
        pins = list(dict.fromkeys(match.group(0) for text in texts for match in VERSION_PIN.finditer(text)))
    return LintResult(bead.model_copy(update=update) if update else bead, dropped, pins, suspicious)


_SHIP_WORDS = re.compile(r"\b(?:ship|release|version\s+bump|bump\w*\s+(?:the\s+)?(?:\w+\s+)?version)\b", re.I)


def is_ship_context(bead: Bead, *, recipe_name: str, graph: str | None) -> bool:
    """A version-bump/ship bead: a `script` (or `ship`) recipe, or a title or
    label that says ship, release or version bump."""
    if graph == "script" or "ship" in recipe_name:
        return True
    return bool(_SHIP_WORDS.search(bead.title) or any(_SHIP_WORDS.search(label) for label in bead.labels))
