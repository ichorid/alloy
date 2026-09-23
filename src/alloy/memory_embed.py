"""``alloy memory embed`` (alloy-4ef.18): render the ``alloy:meta:embed`` set
into a managed block in the repository's instruction files.

Both helpers are pure. ``render_embed_block`` turns a ``ProjectMemory`` into
the block text (markers included) and ``splice_managed_block`` puts that text
into an instruction file's contents, touching nothing outside the markers.
The CLI only reads bd and writes files; git is never involved.
"""

from __future__ import annotations

from datetime import date, timedelta

from alloy.config import MemorySpec
from alloy.models import EMBED_KEY, LAST_REVIEW_KEY, ProjectMemory, _embeddable, _embedded_keys

BEGIN_MARKER = "<!-- alloy:memory:begin -->"
END_MARKER = "<!-- alloy:memory:end -->"


def embed_keys(memory: ProjectMemory) -> list[str]:
    """Keys named by ``alloy:meta:embed`` that exist and may be embedded, sorted."""

    named = _embedded_keys(memory.body_of(EMBED_KEY))
    return sorted(key for key in named if key in memory.entries and _embeddable(key))


def last_review_date(memory: ProjectMemory) -> date | None:
    """The ISO date stored under ``alloy:meta:last-review``; None when absent
    or unparseable."""

    body = memory.body_of(LAST_REVIEW_KEY).strip()
    if not body:
        return None
    try:
        return date.fromisoformat(body)
    except ValueError:
        return None


def render_embed_block(memory: ProjectMemory, spec: MemorySpec) -> str:
    """The managed block: markers around ``reviewed:`` / ``review due:`` header
    lines and the embed set sorted by key with provenance stripped."""

    reviewed = last_review_date(memory)
    if reviewed is None:
        header = ["reviewed: never", "review due: now"]
    else:
        due = reviewed + timedelta(days=spec.review_every_days)
        header = [f"reviewed: {reviewed.isoformat()}", f"review due: {due.isoformat()}"]
    lines = [BEGIN_MARKER, *header]
    for key in embed_keys(memory):
        lines.extend(("", f"### {key}", memory.body_of(key)))
    lines.append(END_MARKER)
    return "\n".join(lines)


def splice_managed_block(source: str, managed: str) -> tuple[str, bool]:
    """Return ``(updated, changed)``: ``source`` with ``managed`` replacing the
    region from the begin marker through the end marker, or appended after a
    blank line when the markers are absent. Bytes outside the markers are
    left untouched; ``changed`` is False when the result equals ``source``."""

    begin = source.find(BEGIN_MARKER)
    end = source.find(END_MARKER, begin + len(BEGIN_MARKER)) if begin >= 0 else -1
    if begin >= 0 and end >= 0:
        updated = source[:begin] + managed + source[end + len(END_MARKER):]
    else:
        head = source.rstrip("\n")
        updated = f"{head}\n\n{managed}\n" if head else f"{managed}\n"
    return updated, updated != source
