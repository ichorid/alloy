"""Codex CLI adapter.

``codex exec --json`` emits one JSON object per line. The agent's answer is the
last ``agent_message`` item; token usage arrives on ``turn.completed``.
"""

from __future__ import annotations

import json
from typing import Any

from alloy.runners.base import CLIRunner


class CodexRunner(CLIRunner):
    name = "codex"
    binary = "codex"
    default_model = None
    sandbox = "workspace-write"

    def build_command(
        self, prompt: str, *, model: str | None, structured_schema: dict | None
    ) -> list[str]:
        args = ["exec", "--json", "--skip-git-repo-check", "--sandbox", self.sandbox]
        if model:
            args += ["--model", model]
        args += self.extra_args
        args.append(prompt)
        return args

    def parse(self, stdout, stderr, exit_code):
        messages: list[str] = []
        usage: dict[str, Any] = {}
        session_id: str | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            kind = event.get("type")
            if kind == "thread.started":
                session_id = event.get("thread_id")
            elif kind == "turn.completed":
                usage = dict(event.get("usage") or {})
            elif kind == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    messages.append(str(item["text"]))
            elif kind == "error":
                messages.append(str(event.get("message", "")))
        text = messages[-1] if messages else stdout.strip()
        return text, None, usage, session_id


class CodexReadOnlyRunner(CodexRunner):
    """Codex restricted to reading -- used for critics, which must not edit code."""

    name = "codex-readonly"
    sandbox = "read-only"
