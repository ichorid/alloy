"""Codex CLI adapter.

``codex exec --json`` emits one JSON object per line. The agent's answer is the
last ``agent_message`` item; token usage arrives on ``turn.completed``.
"""

from __future__ import annotations

import json
import os
from typing import Any

from alloy.runners.base import CLIRunner


class CodexRunner(CLIRunner):
    """Codex with its own `workspace-write` sandbox.

    That sandbox, left at its defaults, blocks every socket and makes
    everything outside the checkout read-only. Implementers then cannot run
    the project's tests: test runners bind localhost (flutter test), pg suites
    and package managers need the network, and SDK/tool caches (~/.cache,
    ~/.pub-cache, the Flutter SDK cache) must be writable. A codex implement
    call once spent its whole 25-minute budget working around exactly that.
    So the network is on by default and extra writable directories are
    configurable per recipe::

        runners:
          codex:
            network_access: true
            writable_roots: [~/.cache, ~/.pub-cache]

    claude-write runs without a sandbox at all; Alloy's per-run bubblewrap
    namespace (alloy.sandbox) still gives every run a private /tmp.
    """

    name = "codex"
    binary = "codex"
    default_model = None
    sandbox = "workspace-write"

    def __init__(
        self,
        binary: str | None = None,
        *,
        network_access: bool = True,
        writable_roots: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(binary, **kwargs)
        self.network_access = bool(network_access)
        self.writable_roots = [os.path.expanduser(str(root)) for root in (writable_roots or [])]

    def _sandbox_args(self) -> list[str]:
        """`-c` overrides for the workspace-write sandbox (none for read-only)."""
        if self.sandbox != "workspace-write":
            return []
        args = ["-c", f"sandbox_workspace_write.network_access={'true' if self.network_access else 'false'}"]
        if self.writable_roots:
            # A TOML array of basic strings; JSON string quoting is valid TOML.
            roots = ",".join(json.dumps(root) for root in self.writable_roots)
            args += ["-c", f"sandbox_workspace_write.writable_roots=[{roots}]"]
        return args

    def build_command(
        self,
        prompt: str,
        *,
        model: str | None,
        structured_schema: dict | None,
        effort: str | None = None,
        resume_session: str | None = None,
        fork_session: str | None = None,
    ) -> list[str]:
        if resume_session or fork_session:
            # `codex exec resume|fork <SESSION_ID> [PROMPT]` (checked against
            # codex-cli 0.164.0). Both take --json, --model and -c but not
            # --sandbox, so the sandbox rides on the config key.
            args = [
                "exec",
                "fork" if fork_session and not resume_session else "resume",
                resume_session or fork_session,
                "--json",
                "--skip-git-repo-check",
                "-c",
                f'sandbox_mode="{self.sandbox}"',
            ]
        else:
            args = [
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--sandbox",
                self.sandbox,
            ]
        if model:
            args += ["--model", model]
        if effort:
            args += ["-c", f'model_reasoning_effort="{effort}"']
        args += self._sandbox_args()
        args += self.extra_args
        args.append(prompt)
        return args

    def build_command_stdin(
        self,
        *,
        model: str | None,
        structured_schema: dict | None,
        effort: str | None = None,
        resume_session: str | None = None,
        fork_session: str | None = None,
    ) -> list[str]:
        return self.build_command(
            "-",
            model=model,
            structured_schema=structured_schema,
            effort=effort,
            resume_session=resume_session,
            fork_session=fork_session,
        )

    def parse(self, stdout, stderr, exit_code):
        messages: list[str] = []
        usage: dict[str, Any] = {}
        session_id: str | None = None
        # An `error` event with no agent_message after it is a failed call
        # even on exit 0 (e.g. "out of credits").
        failed = False
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
                    failed = False
            elif kind == "error":
                messages.append(str(event.get("message", "")))
                failed = True
        text = messages[-1] if messages else stdout.strip()
        return text, None, usage, session_id, failed


class CodexReadOnlyRunner(CodexRunner):
    """Codex restricted to reading -- used for critics, which must not edit code."""

    name = "codex-readonly"
    sandbox = "read-only"
