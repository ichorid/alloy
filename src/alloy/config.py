"""Recipe configuration.

Graph *logic* is Python. YAML only answers "which harness plays which role, and
what are the hard limits" -- the things worth changing without editing code.
There is deliberately no DSL here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from alloy.models import COMPLEXITY_LEVELS

log = logging.getLogger(__name__)

BUILTIN_RECIPE_DIR = Path(__file__).parent / "recipes"

DEFAULT_ROLE_TIMEOUT_MIN = 20.0

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Cursor has no effort knob: the effort lives in the model id (kimi-k3-high).
_RUNNERS_WITHOUT_EFFORT = ("cursor", "cursor-plan")


@dataclass(frozen=True)
class RoleSpec:
    runner: str
    model: str | None = None
    timeout_minutes: float = DEFAULT_ROLE_TIMEOUT_MIN
    fallback: "RoleSpec | None" = None
    """Used when the primary runner is unavailable or exits non-zero.

    A fallback is a whole RoleSpec (runner, model, timeout, and possibly its
    own fallback), so `implement: {runner: astra, fallback: {runner:
    claude-write, model: fable}}` hands the same prompt to Claude when the
    Codex CLI is missing, rate-limited, or times out.
    """
    tiered: bool = False
    effort: str | None = None
    """Reasoning effort for this entry (claude ``--effort``, codex
    ``model_reasoning_effort``). Per entry, not a global default, so a tier can
    pair a cheap model with low effort and its fallback with high."""

    @property
    def timeout(self) -> timedelta:
        return timedelta(minutes=self.timeout_minutes)

    @property
    def label(self) -> str:
        label = f"{self.runner}:{self.model}" if self.model else self.runner
        return f"{label}@{self.effort}" if self.effort else label

    @classmethod
    def parse(cls, raw: Any, *, default_runner: str = "claude") -> "RoleSpec":
        if isinstance(raw, str):
            return cls(runner=raw)
        raw = raw or {}
        fallback_raw = raw.get("fallback")
        effort = raw.get("effort")
        if effort is not None and effort not in EFFORT_LEVELS:
            raise ConfigError(
                f"invalid effort {effort!r}; expected one of {', '.join(EFFORT_LEVELS)}"
            )
        return cls(
            runner=raw.get("runner", default_runner),
            model=raw.get("model"),
            timeout_minutes=float(raw.get("timeout_minutes", DEFAULT_ROLE_TIMEOUT_MIN)),
            fallback=cls.parse(fallback_raw, default_runner=default_runner)
            if fallback_raw else None,
            tiered=bool(raw.get("tiered", False)),
            effort=effort,
        )


@dataclass(frozen=True)
class ComplexitySpec:
    routing: str = "shadow"
    escalate_after_retries: int = 2
    tiers: dict[str, RoleSpec] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> "ComplexitySpec":
        raw = raw or {}
        routing = raw.get("routing", "shadow")
        if routing not in ("shadow", "live"):
            raise ConfigError(f"invalid complexity routing: {routing!r}")
        try:
            retries = int(raw.get("escalate_after_retries", 2))
        except (TypeError, ValueError) as exc:
            raise ConfigError("complexity escalate_after_retries must be an integer") from exc
        tiers = {}
        for level, entries in (raw.get("tiers") or {}).items():
            if level not in COMPLEXITY_LEVELS:
                raise ConfigError(f"unknown complexity tier: {level!r}")
            if not isinstance(entries, list) or not entries:
                raise ConfigError(f"complexity tier {level!r} must be a non-empty list")
            chain = None
            for entry in reversed(entries):
                if not isinstance(entry, dict):
                    raise ConfigError(f"complexity tier {level!r} entries must be mappings")
                spec = RoleSpec.parse(entry)
                if spec.effort and spec.runner in _RUNNERS_WITHOUT_EFFORT:
                    raise ConfigError(
                        f"complexity tier {level!r}: runner {spec.runner!r} has no effort "
                        "knob; encode it in the model id instead"
                    )
                chain = replace(spec, fallback=chain)
            tiers[level] = chain
        return cls(routing=routing, escalate_after_retries=retries, tiers=tiers)


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
class VerificationSpec:
    """Hard limits on the checks Alloy will run; never what the checks are."""

    max_checks_per_iteration: int = 5
    max_total_checks: int = 20
    max_command_timeout_minutes: float = 15.0
    max_baseline_repairs: int = 2
    min_acceptance_confidence: float = 0.6

    @classmethod
    def parse(
        cls, raw: dict[str, Any] | None, *, legacy: VerifySpec | None = None
    ) -> "VerificationSpec":
        raw = raw or {}
        defaults = cls()
        timeout = raw.get("max_command_timeout_minutes")
        if timeout is None:
            timeout = legacy.timeout_minutes if legacy else defaults.max_command_timeout_minutes
        return cls(
            max_checks_per_iteration=int(
                raw.get("max_checks_per_iteration", defaults.max_checks_per_iteration)
            ),
            max_total_checks=int(raw.get("max_total_checks", defaults.max_total_checks)),
            max_command_timeout_minutes=float(timeout),
            max_baseline_repairs=int(raw.get("max_baseline_repairs", defaults.max_baseline_repairs)),
            min_acceptance_confidence=float(
                raw.get("min_acceptance_confidence", defaults.min_acceptance_confidence)
            ),
        )


@dataclass(frozen=True)
class RecipeConfig:
    name: str
    roles: dict[str, RoleSpec]
    consilium: ConsiliumSpec
    limits: Limits
    verify: VerifySpec
    verification: VerificationSpec = field(default_factory=VerificationSpec)
    runners: dict[str, dict[str, Any]] = field(default_factory=dict)
    on_success_status: str = "review-ready"
    cleanup_worktree_on_success: bool = False
    source_path: Path | None = None
    complexity: ComplexitySpec = field(default_factory=ComplexitySpec)

    def role(self, name: str) -> RoleSpec:
        try:
            return self.roles[name]
        except KeyError as exc:
            raise ConfigError(f"recipe '{self.name}' has no role '{name}'") from exc

    def resolve_role(self, name: str, complexity: str | None) -> RoleSpec:
        spec = self.role(name)
        if not spec.tiered or self.complexity.routing != "live":
            return spec
        if complexity not in COMPLEXITY_LEVELS:
            raise ConfigError(
                f"tiered role '{name}' requires a known complexity level, got {complexity!r}"
            )
        try:
            return self.complexity.tiers[complexity]
        except KeyError as exc:
            raise ConfigError(f"recipe '{self.name}' has no complexity tier '{complexity}'") from exc

    @classmethod
    def parse(cls, raw: dict[str, Any], *, source: Path | None = None) -> "RecipeConfig":
        roles_raw = raw.get("roles") or {}
        roles = {key: RoleSpec.parse(value) for key, value in roles_raw.items()}
        complexity = ComplexitySpec.parse(raw.get("complexity"))
        if any(spec.tiered for spec in roles.values()) and not complexity.tiers:
            raise ConfigError("tiered roles require complexity tiers")
        verify = VerifySpec.parse(raw.get("verify"))
        if verify.command:
            log.warning("verify.command is deprecated and ignored")
        return cls(
            name=raw.get("name") or (source.stem if source else "unnamed"),
            roles=roles,
            complexity=complexity,
            consilium=ConsiliumSpec.parse(raw.get("consilium")),
            limits=Limits.parse(raw.get("limits")),
            verify=verify,
            verification=VerificationSpec.parse(raw.get("verification"), legacy=verify),
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
