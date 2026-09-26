"""Claude Code CLI adapter.

Uses the user's existing Claude Code authentication; no API key is required.
Claude is the only harness here with native structured output (``--json-schema``).
"""

from __future__ import annotations

import json
from typing import Any

from alloy.runners.base import CLIRunner


class ClaudeRunner(CLIRunner):
    name = "claude"
    binary = "claude"
    default_model = "sonnet"
    supports_native_schema = True

    def build_command(
        self,
        prompt: str,
        *,
        model: str | None,
        structured_schema: dict | None,
        effort: str | None = None,
        resume_session: str | None = None,
    ) -> list[str]:
        args: list[str] = []
        if resume_session:
            # `--resume <session-id>` (checked against claude 2.1.280).
            args += ["--resume", resume_session]
        args += ["-p", prompt, "--output-format", "json"]
        if model:
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]
        if structured_schema:
            args += ["--json-schema", json.dumps(structured_schema)]
        args += self.extra_args
        return args

    def build_command_stdin(
        self,
        *,
        model: str | None,
        structured_schema: dict | None,
        effort: str | None = None,
        resume_session: str | None = None,
    ) -> list[str]:
        # Reuse the command builder to retain ClaudeWriteRunner's permissions.
        args = self.build_command(
            "",
            model=model,
            structured_schema=structured_schema,
            effort=effort,
            resume_session=resume_session,
        )
        del args[args.index("-p") + 1]
        return args

    def parse(self, stdout, stderr, exit_code):
        try:
            envelope = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return stdout.strip(), None, {}, None, False
        if not isinstance(envelope, dict):
            return stdout.strip(), None, {}, None, False

        text = envelope.get("result") or ""
        structured = envelope.get("structured_output")
        if not isinstance(structured, dict):
            structured = None
        usage: dict[str, Any] = dict(envelope.get("usage") or {})
        if "total_cost_usd" in envelope:
            usage["total_cost_usd"] = envelope["total_cost_usd"]
        if "num_turns" in envelope:
            usage["num_turns"] = envelope["num_turns"]
        failed = bool(envelope.get("is_error"))
        if failed:
            text = text or json.dumps(envelope.get("error", envelope))[:2000]
        return str(text), structured, usage, envelope.get("session_id"), failed


class ClaudeWriteRunner(ClaudeRunner):
    """Claude with file-editing permission, for roles that must change the worktree."""

    name = "claude-write"

    def build_command(self, prompt, *, model, structured_schema, effort=None, resume_session=None):
        return [
            "--permission-mode",
            "bypassPermissions",
            *super().build_command(
                prompt,
                model=model,
                structured_schema=structured_schema,
                effort=effort,
                resume_session=resume_session,
            ),
        ]
