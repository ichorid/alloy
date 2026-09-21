"""Recipe configuration.

Graph *logic* is Python. YAML only answers "which harness plays which role, and
what are the hard limits" -- the things worth changing without editing code.
There is deliberately no DSL here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

BUILTIN_RECIPE_DIR = Path(__file__).parent / "recipes"

DEFAULT_ROLE_TIMEOUT_MIN = 20.0


@dataclass(frozen=True)
class RoleSpec:
    runner: str
    model: str | None = None
    timeout_minutes: float = DEFAULT_ROLE_TIMEOUT_MIN

    @property
    def timeout(self) -> timedelta:
        return timedelta(minutes=self.timeout_minutes)

    @classmethod
    def parse(cls, raw: Any, *, default_runner: str = "claude") -> "RoleSpec":
        if isinstance(raw, str):
            return cls(runner=raw)
        raw = raw or {}
        return cls(
            runner=raw.get("runner", default_runner),
            model=raw.get("model"),
            timeout_minutes=float(raw.get("timeout_minutes", DEFAULT_ROLE_TIMEOUT_MIN)),
        )


@dataclass(frozen=True)
class Limits:
    """Hard stops Alloy enforces itself. An agent can never opt out of these."""

    max_iterations: int = 5
    max_consiliums: int = 1
    max_wall_time_minutes: float = 90.0
    max_agent_calls: int = 20

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> "Limits":
        raw = raw or {}
        defaults = cls()
        return cls(
            max_iterations=int(raw.get("max_iterations", defaults.max_iterations)),
            max_consiliums=int(raw.get("max_consiliums", defaults.max_consiliums)),
            max_wall_time_minutes=float(
                raw.get("max_wall_time_minutes", defaults.max_wall_time_minutes)
            ),
            max_agent_calls=int(raw.get("max_agent_calls", defaults.max_agent_calls)),
        )


@dataclass(frozen=True)
class ConsiliumSpec:
    critics: tuple[RoleSpec, ...] = ()
    synthesizer: RoleSpec = field(default_factory=lambda: RoleSpec("claude"))

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> "ConsiliumSpec":
        raw = raw or {}
        critics = tuple(RoleSpec.parse(item) for item in (raw.get("critics") or []))
        return cls(critics=critics, synthesizer=RoleSpec.parse(raw.get("synthesizer")))


@dataclass(frozen=True)
class VerifySpec:
    command: str | None = None
    timeout_minutes: float = 15.0

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> "VerifySpec":
        raw = raw or {}
        return cls(
            command=raw.get("command"),
            timeout_minutes=float(raw.get("timeout_minutes", 15.0)),
        )


@dataclass(frozen=True)
class RecipeConfig:
    name: str
    roles: dict[str, RoleSpec]
    consilium: ConsiliumSpec
    limits: Limits
    verify: VerifySpec
    runners: dict[str, dict[str, Any]] = field(default_factory=dict)
    on_success_status: str = "review-ready"
    cleanup_worktree_on_success: bool = False
    source_path: Path | None = None

    def role(self, name: str) -> RoleSpec:
        try:
            return self.roles[name]
        except KeyError as exc:
            raise ConfigError(f"recipe '{self.name}' has no role '{name}'") from exc

    @classmethod
    def parse(cls, raw: dict[str, Any], *, source: Path | None = None) -> "RecipeConfig":
        roles_raw = raw.get("roles") or {}
        return cls(
            name=raw.get("name") or (source.stem if source else "unnamed"),
            roles={key: RoleSpec.parse(value) for key, value in roles_raw.items()},
            consilium=ConsiliumSpec.parse(raw.get("consilium")),
            limits=Limits.parse(raw.get("limits")),
            verify=VerifySpec.parse(raw.get("verify")),
            runners=dict(raw.get("runners") or {}),
            on_success_status=raw.get("on_success_status", "review-ready"),
            cleanup_worktree_on_success=bool(raw.get("cleanup_worktree_on_success", False)),
            source_path=source,
        )


class ConfigError(RuntimeError):
    pass


def recipe_search_path(alloy_root: Path | None = None, project: Path | None = None) -> list[Path]:
    """Later entries win: built-ins, then user overrides, then project-local."""
    paths = [BUILTIN_RECIPE_DIR]
    if alloy_root:
        paths.append(Path(alloy_root) / "recipes")
    if project:
        paths.append(Path(project) / ".alloy" / "recipes")
    return paths


def discover_recipes(
    alloy_root: Path | None = None, project: Path | None = None
) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for directory in recipe_search_path(alloy_root, project):
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob("*.y*ml")):
            found[candidate.stem] = candidate
    return found


def load_recipe(
    name: str, *, alloy_root: Path | None = None, project: Path | None = None
) -> RecipeConfig:
    available = discover_recipes(alloy_root, project)
    path = available.get(name)
    if path is None:
        known = ", ".join(sorted(available)) or "(none)"
        raise ConfigError(f"unknown recipe '{name}'; available: {known}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"recipe file {path} must contain a mapping")
    raw.setdefault("name", name)
    return RecipeConfig.parse(raw, source=path)
