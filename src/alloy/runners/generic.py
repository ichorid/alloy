"""Template-driven adapter for harnesses Alloy has no bespoke knowledge of.

`pi` is wired up this way, and so is any future CLI: describe its invocation in
recipe config and Alloy can drive it without new code.

    runners:
      pi:
        binary: pi
        args: ["--print", "--json", "{prompt}"]
        model_args: ["--model", "{model}"]
        output: json_envelope
        text_field: result
        resume_flag: --session      # optional; renders `{resume_args}` in args
"""

from __future__ import annotations

import json
from typing import Any

from alloy.runners.base import CLIRunner, extract_json_object


class GenericCLIRunner(CLIRunner):
    """Builds argv from a template; parses plain text, a JSON envelope, or JSONL."""

    name = "generic"

    def __init__(
        self,
        binary: str,
        *,
        name: str | None = None,
        args: list[str] | None = None,
        model_args: list[str] | None = None,
        output: str = "text",
        text_field: str = "result",
        usage_field: str = "usage",
        resume_flag: str | None = None,
        default_model: str | None = None,
        env: dict[str, str] | None = None,
        log_dir=None,
    ) -> None:
        super().__init__(binary, default_model=default_model, env=env, log_dir=log_dir)
        self.name = name or binary
        self.args_template = list(args or ["{prompt}"])
        self.model_args_template = list(model_args or [])
        self.output = output
        self.text_field = text_field
        self.usage_field = usage_field
        self.resume_flag = resume_flag

    def build_command(
        self,
        prompt: str,
        *,
        model: str | None,
        structured_schema: dict | None,
        resume_session: str | None = None,
    ) -> list[str]:
        # `{resume_args}` expands to `<resume_flag> <id>` when resuming and to
        # nothing at all otherwise -- never to an empty argument.
        resume_args: list[str] = []
        if resume_session and self.resume_flag:
            resume_args = [self.resume_flag, resume_session]
        args: list[str] = []
        for part in self.args_template:
            if part == "{resume_args}":
                args += resume_args
            else:
                args.append(part.replace("{prompt}", prompt))
        if model and self.model_args_template:
            args += [part.replace("{model}", model) for part in self.model_args_template]
        return args + self.extra_args

    def parse(self, stdout, stderr, exit_code):
        if self.output == "text":
            return stdout.strip(), extract_json_object(stdout), {}, None
        envelope = self._envelope(stdout)
        if envelope is None:
            return stdout.strip(), extract_json_object(stdout), {}, None
        text = envelope.get(self.text_field)
        usage = envelope.get(self.usage_field) or {}
        return (
            str(text) if text is not None else stdout.strip(),
            None,
            dict(usage) if isinstance(usage, dict) else {},
            envelope.get("session_id") or envelope.get("thread_id"),
        )

    def _envelope(self, stdout: str) -> dict[str, Any] | None:
        if self.output == "json_envelope":
            try:
                parsed = json.loads(stdout)
            except (json.JSONDecodeError, ValueError):
                return None
            return parsed if isinstance(parsed, dict) else None
        if self.output == "jsonl":
            last: dict[str, Any] | None = None
            for line in stdout.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    candidate = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(candidate, dict) and self.text_field in candidate:
                    last = candidate
            return last
        return None


class PiRunner(GenericCLIRunner):
    """Pi coding agent. Defaults follow Pi's non-interactive print mode."""

    def __init__(self, **kwargs: Any) -> None:
        defaults: dict[str, Any] = {
            "binary": "pi",
            "name": "pi",
            "args": ["--print", "--output-format", "json", "{prompt}"],
            "model_args": ["--model", "{model}"],
            "output": "json_envelope",
            "text_field": "result",
        }
        defaults.update(kwargs)
        super().__init__(**defaults)
