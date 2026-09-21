"""Runner registry.

The graph names a runner ("claude", "codex", ...); this module decides what that
means. Swapping a harness is a config edit, never a graph edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from alloy.models import RunnerUnavailable
from alloy.runners.base import AgentRunner, CLIRunner
from alloy.runners.claude import ClaudeRunner, ClaudeWriteRunner
from alloy.runners.codex import CodexReadOnlyRunner, CodexRunner
from alloy.runners.cursor import CursorPlanRunner, CursorRunner
from alloy.runners.generic import GenericCLIRunner, PiRunner

BUILTIN: dict[str, Callable[..., CLIRunner]] = {
    "claude": ClaudeRunner,
    "claude-write": ClaudeWriteRunner,
    "codex": CodexRunner,
    "codex-readonly": CodexReadOnlyRunner,
    "cursor": CursorRunner,
    "cursor-plan": CursorPlanRunner,
    "pi": PiRunner,
    "generic": GenericCLIRunner,
}

# "astra" is the implementation persona the recipes refer to; it rides on Codex
# unless a user overrides it in config.
ALIASES: dict[str, str] = {"astra": "codex"}


class RunnerRegistry:
    """Resolves runner names to configured instances, one instance per name."""

    def __init__(
        self,
        overrides: dict[str, dict[str, Any]] | None = None,
        *,
        log_dir: Path | None = None,
    ) -> None:
        self.overrides = dict(overrides or {})
        self.log_dir = log_dir
        self._cache: dict[str, CLIRunner] = {}

    def get(self, name: str) -> CLIRunner:
        key = ALIASES.get(name, name)
        if key in self._cache:
            return self._cache[key]
        config = dict(self.overrides.get(name) or self.overrides.get(key) or {})
        factory_name = config.pop("type", key)
        factory = BUILTIN.get(factory_name)
        if factory is None:
            if "binary" not in config:
                raise RunnerUnavailable(
                    f"unknown runner '{name}'; define it under `runners:` with a binary"
                )
            factory = GenericCLIRunner
            config.setdefault("name", name)
        if self.log_dir is not None:
            config.setdefault("log_dir", self.log_dir)
        runner = factory(**config)
        runner.name = config.get("name", runner.name)
        self._cache[key] = runner
        return runner

    def available(self, name: str) -> bool:
        try:
            return self.get(name).available()
        except RunnerUnavailable:
            return False

    def with_log_dir(self, log_dir: Path) -> "RunnerRegistry":
        return RunnerRegistry(self.overrides, log_dir=log_dir)


__all__ = [
    "AgentRunner",
    "CLIRunner",
    "RunnerRegistry",
    "RunnerUnavailable",
    "BUILTIN",
    "ALIASES",
]
