"""Clipping unified diffs for prompts without letting one file hide the rest.

A single generated file (a 1,480-line uv.lock, journal 33) can eat the whole
diff budget when the diff is clipped wholesale. Clip each file's section first,
then the total, and say which files were cut.
"""

from __future__ import annotations

import re

from alloy.models import clip

_FILE_HEADER = re.compile(r"(?m)^(?=diff --git )")


def _path_of(section: str) -> str:
    first = section.split("\n", 1)[0]
    match = re.match(r"diff --git a/(.*?) b/(.*)$", first)
    return match.group(2) if match else first


def clip_diff_per_file(diff: str, per_file: int, total: int) -> str:
    """Clip each `diff --git` section to `per_file` chars (via `models.clip`),
    then the concatenation to `total`, appending one line naming the cut files."""
    sections = [s for s in _FILE_HEADER.split(diff) if s]
    clipped_files: list[str] = []
    out: list[str] = []
    for section in sections:
        clipped = clip(section, per_file)
        if clipped != section:
            clipped_files.append(_path_of(section))
        out.append(clipped)
    text = clip("".join(out), total)
    if not clipped_files:
        return text
    return text.rstrip("\n") + f"\n[clipped to {per_file} chars per file: {', '.join(clipped_files)}]"
