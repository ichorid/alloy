"""Cursor CLI adapter.

``cursor-agent -p --output-format json`` returns a single result envelope.
``--force`` is required for non-interactive use in an untrusted directory --
Alloy always runs it inside a throwaway worktree.
"""

from __future__ import annotations

import json
from typing import Any

from alloy.runners.base import CLIRunner


class CursorRunner(CLIRunner):
    name = "cursor"
    binary = "cursor-agent"
    default_model = None
    read_only = False

    def build_command(
        self, prompt: str, *, model: str | None, structured_schema: dict | None
    ) -> list[str]:
        args = ["-p", prompt, "--output-format", "json", "--force"]
        if self.read_only:
            args += ["--mode", "plan"]
        if model:
            args += ["--model", model]
        args += self.extra_args
        return args

    def parse(self, stdout, stderr, exit_code):
        envelope: dict[str, Any] | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                candidate = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(candidate, dict) and candidate.get("type") == "result":
                envelope = candidate
        if envelope is None:
            return stdout.strip(), None, {}, None, False
        text = str(envelope.get("result") or "")
        usage = dict(envelope.get("usage") or {})
        failed = bool(envelope.get("is_error"))
        if failed:
            text = text or "cursor reported an error"
        return text, None, usage, envelope.get("session_id"), failed


class CursorPlanRunner(CursorRunner):
    """Read-only Cursor, for context gathering and critique."""

    name = "cursor-plan"
    read_only = True
