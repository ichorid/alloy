"""Recipe configuration.

Graph *logic* is Python. YAML only answers "which harness plays which role, and
what are the hard limits" -- the things worth changing without editing code.
There is deliberately no DSL here.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from alloy.models import COMPLEXITY_LEVELS, _load_calibration
from alloy.sandbox import SandboxSpec

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
    panel: "tuple[RoleSpec, ...]" = ()
    """Independent members that all run for this role (tests_review only). Each
    is one whole RoleSpec; `fallback` then applies to the panel as a whole, used
    only when no member produced a usable answer."""

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
            raise ConfigError(f"invalid effort {effort!r}; expected one of {', '.join(EFFORT_LEVELS)}")
        return cls(
            runner=raw.get("runner", default_runner),
            model=raw.get("model"),
            timeout_minutes=float(raw.get("timeout_minutes", DEFAULT_ROLE_TIMEOUT_MIN)),
            fallback=cls.parse(fallback_raw, default_runner=default_runner) if fallback_raw else None,
            tiered=bool(raw.get("tiered", False)),
            effort=effort,
            panel=tuple(cls.parse(item, default_runner=default_runner) for item in (raw.get("panel") or [])),
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


DEFAULT_CHEAP_ROLES: tuple[str, ...] = ("verifier", "acceptance", "harvest", "triage", "estimate", "judge")
"""Roles whose calls spend `limits.max_cheap_agent_calls` instead of
`limits.max_agent_calls`: short, bounded calls that a long verify loop makes
many of, and that should not starve the implementer of budget."""


def _tier_map(raw: dict[str, Any], key: str) -> dict[str, int]:
    by_tier: dict[str, int] = {}
    for level, value in (raw.get(key) or {}).items():
        if level not in COMPLEXITY_LEVELS:
            raise ConfigError(f"unknown {key} tier: {level!r}")
        by_tier[level] = int(value)
    return by_tier


@dataclass(frozen=True)
class Limits:
    """Hard stops Alloy enforces itself. An agent can never opt out of these."""

    max_iterations: int = 5
    max_consiliums: int = 1
    max_wall_time_minutes: float = 90.0
    max_agent_calls: int = 20
    max_agent_calls_by_tier: dict[str, int] = field(default_factory=dict)
    max_cheap_agent_calls: int = 40
    """Ceiling on calls of `cheap_roles`; they never count toward
    `max_agent_calls`, and the rest never count toward this one."""
    max_cheap_agent_calls_by_tier: dict[str, int] = field(default_factory=dict)
    cheap_roles: tuple[str, ...] = DEFAULT_CHEAP_ROLES
    timeout_resumes: int = 1
    """How many times a call stopped at its role's time limit continues its
    own session (warm prompt cache, its edits still in the tree) before the
    chain moves on to the fallback. 0 = fall back at once, as before."""
    job_wait_minutes: float = 20.0
    """Longest single `alloy job wait` of an agent (and, for Claude Code,
    its shell-tool timeout): short enough that each return from a wait finds
    the agent's prompt cache (about 30 min) still warm. See alloy.jobs."""
    job_abandon_minutes: float = 45.0
    """A job nobody has waited on for this long is killed as abandoned --
    the backstop for an agent call whose end Alloy never saw."""

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> "Limits":
        raw = raw or {}
        defaults = cls()
        cheap_roles = raw.get("cheap_roles", defaults.cheap_roles)
        if isinstance(cheap_roles, str) or not all(isinstance(role, str) for role in cheap_roles or ()):
            raise ConfigError(f"limits.cheap_roles must be a list of role names: {cheap_roles!r}")
        job_wait = float(raw.get("job_wait_minutes", defaults.job_wait_minutes))
        job_abandon = float(raw.get("job_abandon_minutes", defaults.job_abandon_minutes))
        if not 0 < job_wait < job_abandon:
            raise ConfigError(
                f"limits: need 0 < job_wait_minutes ({job_wait:g}) < job_abandon_minutes ({job_abandon:g}), "
                "or a job is killed as abandoned between two waits"
            )
        return cls(
            max_iterations=int(raw.get("max_iterations", defaults.max_iterations)),
            max_consiliums=int(raw.get("max_consiliums", defaults.max_consiliums)),
            max_wall_time_minutes=float(raw.get("max_wall_time_minutes", defaults.max_wall_time_minutes)),
            max_agent_calls=int(raw.get("max_agent_calls", defaults.max_agent_calls)),
            max_agent_calls_by_tier=_tier_map(raw, "max_agent_calls_by_tier"),
            max_cheap_agent_calls=int(raw.get("max_cheap_agent_calls", defaults.max_cheap_agent_calls)),
            max_cheap_agent_calls_by_tier=_tier_map(raw, "max_cheap_agent_calls_by_tier"),
            cheap_roles=tuple(cheap_roles or ()),
            timeout_resumes=max(0, int(raw.get("timeout_resumes", defaults.timeout_resumes))),
            job_wait_minutes=job_wait,
            job_abandon_minutes=job_abandon,
        )

    def is_cheap_role(self, role: str) -> bool:
        """`critic:codex`-style names match on the part before the colon."""
        return role in self.cheap_roles or role.split(":", 1)[0] in self.cheap_roles


DEFAULT_TIER_AGENT_CALL_MULTIPLIER = 1.5
"""Calibration mean multiplier behind the per-tier agent-call ceiling."""


def resolve_max_agent_calls(config: "RecipeConfig", complexity: str | None, calibration_body: str = "") -> int:
    """Agent-call ceiling for a run of the given complexity tier.

    The flat ``limits.max_agent_calls`` is the floor. A known tier raises it
    to whichever is higher: the recipe's explicit
    ``limits.max_agent_calls_by_tier`` value, or
    DEFAULT_TIER_AGENT_CALL_MULTIPLIER x the stored alloy:calibration
    mean_agent_calls for that tier. A run without a complexity estimate yet
    gets the flat limit.
    """
    limits = config.limits
    if complexity not in COMPLEXITY_LEVELS:
        return limits.max_agent_calls
    ceiling = max(
        limits.max_agent_calls,
        limits.max_agent_calls_by_tier.get(complexity, limits.max_agent_calls),
    )
    entry = _load_calibration(calibration_body).get(complexity) or {}
    mean = float(entry.get("mean_agent_calls", 0.0) or 0.0)
    if mean > 0:
        ceiling = max(ceiling, math.ceil(mean * DEFAULT_TIER_AGENT_CALL_MULTIPLIER))
    return ceiling


def resolve_max_cheap_agent_calls(config: "RecipeConfig", complexity: str | None) -> int:
    """Cheap-role call ceiling for a run of the given complexity tier: the flat
    ``limits.max_cheap_agent_calls``, raised (never lowered) by the tier's
    ``limits.max_cheap_agent_calls_by_tier`` value."""
    limits = config.limits
    if complexity not in COMPLEXITY_LEVELS:
        return limits.max_cheap_agent_calls
    return max(
        limits.max_cheap_agent_calls,
        limits.max_cheap_agent_calls_by_tier.get(complexity, limits.max_cheap_agent_calls),
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
class VerificationSpec:
    """Hard limits on the checks Alloy will run; never what the checks are."""

    max_checks_per_iteration: int = 5
    max_total_checks: int = 20
    max_command_timeout_minutes: float = 15.0
    max_baseline_repairs: int = 2
    max_test_reviews: int = 1
    min_acceptance_confidence: float = 0.6

    @classmethod
    def parse(cls, raw: dict[str, Any] | None, *, legacy: dict[str, Any] | None = None) -> "VerificationSpec":
        """`legacy` is the deprecated `verify:` mapping; only its
        `timeout_minutes` still means anything."""
        raw = raw or {}
        legacy = legacy or {}
        defaults = cls()
        timeout = raw.get("max_command_timeout_minutes")
        if timeout is None:
            timeout = legacy.get("timeout_minutes", defaults.max_command_timeout_minutes)
        return cls(
            max_checks_per_iteration=int(raw.get("max_checks_per_iteration", defaults.max_checks_per_iteration)),
            max_total_checks=int(raw.get("max_total_checks", defaults.max_total_checks)),
            max_command_timeout_minutes=float(timeout),
            max_baseline_repairs=int(raw.get("max_baseline_repairs", defaults.max_baseline_repairs)),
            max_test_reviews=int(raw.get("max_test_reviews", defaults.max_test_reviews)),
            min_acceptance_confidence=float(raw.get("min_acceptance_confidence", defaults.min_acceptance_confidence)),
        )


@dataclass(frozen=True)
class MemorySpec:
    enabled: bool = True
    max_items: int = 12
    max_chars: int = 4000
    ttl_days: int = 90
    harvest_min_confidence: float = 0.7
    review_every_days: int = 7
    review_every_runs: int = 20
    instruction_files: list[str] = field(default_factory=lambda: ["AGENTS.md", "CLAUDE.md"])

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> "MemorySpec":
        if raw is not None and not isinstance(raw, dict):
            raise ConfigError(f"memory must be a mapping: {raw!r}")
        raw = raw or {}
        defaults = cls()
        unknown = raw.keys() - defaults.__dict__.keys()
        if unknown:
            raise ConfigError(f"unknown memory keys: {', '.join(sorted(map(repr, unknown)))}")
        values = defaults.__dict__ | raw
        for name, default in defaults.__dict__.items():
            if type(default) in (int, float):
                try:
                    values[name] = type(default)(values[name])
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ConfigError(f"invalid memory.{name}: {values[name]!r}") from exc
        if not isinstance(values["enabled"], bool):
            raise ConfigError(f"invalid memory.enabled: {values['enabled']!r}")
        instruction_files = values["instruction_files"]
        if not isinstance(instruction_files, list) or not all(isinstance(path, str) for path in instruction_files):
            raise ConfigError(f"invalid memory.instruction_files: {instruction_files!r}")
        values["instruction_files"] = list(instruction_files)
        return cls(**values)


@dataclass(frozen=True)
class CommandSpec:
    """One operator-declared shell command (a `script` recipe's `prepare:` or
    `checks:` entry). Alloy runs it as written; no agent ever chooses it."""

    cmd: str
    purpose: str = ""
    timeout_minutes: float | None = None
    """None: `verification.max_command_timeout_minutes`."""

    @classmethod
    def parse(cls, raw: Any, *, where: str) -> "CommandSpec":
        if isinstance(raw, str):
            raw = {"cmd": raw}
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} entries must be a command string or a mapping with 'cmd': {raw!r}")
        cmd = raw.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ConfigError(f"{where} entry needs a non-empty 'cmd': {raw!r}")
        timeout = raw.get("timeout_minutes")
        try:
            timeout = float(timeout) if timeout is not None else None
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{where} entry has an invalid timeout_minutes: {timeout!r}") from exc
        return cls(cmd=cmd.strip(), purpose=str(raw.get("purpose") or ""), timeout_minutes=timeout)


def _parse_commands(raw: Any, *, where: str) -> tuple[CommandSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"recipe '{where}' must be a list of commands, got {raw!r}")
    return tuple(CommandSpec.parse(item, where=where) for item in raw)


@dataclass(frozen=True)
class LandingGateEntry:
    """One `landing_gate:` entry: when the bead's diff touches a path matching
    any of `paths` (globs relative to the repository; `**` spans directories,
    `*` stays within one), every one of `checks` must pass on the final tree
    before the bead may finish done."""

    paths: tuple[str, ...]
    checks: tuple[CommandSpec, ...]

    @classmethod
    def parse(cls, raw: Any) -> "LandingGateEntry":
        if not isinstance(raw, dict):
            raise ConfigError(f"landing_gate entries must be mappings with 'paths' and 'checks': {raw!r}")
        paths = raw.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p.strip() for p in paths):
            raise ConfigError(f"landing_gate entry needs a non-empty 'paths' list of globs: {raw!r}")
        checks = _parse_commands(raw.get("checks"), where="landing_gate checks")
        if not checks:
            raise ConfigError(f"landing_gate entry needs at least one entry in 'checks': {raw!r}")
        return cls(paths=tuple(p.strip() for p in paths), checks=checks)


def _parse_landing_gate(raw: Any) -> tuple[LandingGateEntry, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"recipe 'landing_gate' must be a list of {{paths, checks}} entries, got {raw!r}")
    return tuple(LandingGateEntry.parse(item) for item in raw)


@dataclass(frozen=True)
class EscalateSpec:
    """fast-track's `escalate:` block: when to hand a mis-sized bead to a
    fuller recipe, exactly as `alloy reroute <bead> <to>` would."""

    to: str
    after_iterations: int | None = None
    """Reroute once this many implement rounds ended without done (retry)."""
    when_packages_touched_over: int | None = None
    """Reroute once the diff touches more distinct `package_roots` than this."""
    package_roots: tuple[str, ...] = ()
    """Globs naming one package root each (`packages/*`): a changed path inside
    a matching directory counts toward that root."""

    @classmethod
    def parse(cls, raw: Any) -> "EscalateSpec | None":
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ConfigError(f"recipe 'escalate' must be a mapping, got {raw!r}")
        unknown = raw.keys() - {"to", "after_iterations", "when_packages_touched_over", "package_roots"}
        if unknown:
            raise ConfigError(f"unknown escalate keys: {', '.join(sorted(map(repr, unknown)))}")
        to = raw.get("to")
        if not isinstance(to, str) or not to.strip():
            raise ConfigError(f"escalate needs 'to': the recipe to reroute the bead to, got {to!r}")
        try:
            after = int(raw["after_iterations"]) if raw.get("after_iterations") is not None else None
            over = int(raw["when_packages_touched_over"]) if raw.get("when_packages_touched_over") is not None else None
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"escalate thresholds must be integers: {raw!r}") from exc
        if after is None and over is None:
            raise ConfigError("escalate needs a trigger: after_iterations and/or when_packages_touched_over")
        roots = raw.get("package_roots") or []
        if isinstance(roots, str):
            roots = [roots]
        if not isinstance(roots, list) or not all(isinstance(root, str) and root.strip() for root in roots):
            raise ConfigError(f"escalate package_roots must be a list of globs, got {roots!r}")
        if over is not None and not roots:
            raise ConfigError("escalate when_packages_touched_over needs package_roots (e.g. ['packages/*'])")
        return cls(
            to=to.strip(),
            after_iterations=after,
            when_packages_touched_over=over,
            package_roots=tuple(root.strip().strip("/") for root in roots),
        )


SCRIPT_GRAPH = "script"
"""The registered graph that runs `prepare:` and `checks:` with no agent."""


@dataclass(frozen=True)
class RecipeConfig:
    name: str
    roles: dict[str, RoleSpec]
    consilium: ConsiliumSpec
    limits: Limits
    verification: VerificationSpec = field(default_factory=VerificationSpec)
    runners: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_path: Path | None = None
    complexity: ComplexitySpec = field(default_factory=ComplexitySpec)
    memory: MemorySpec = field(default_factory=MemorySpec)
    sandbox: SandboxSpec = field(default_factory=SandboxSpec)
    """Per-run bubblewrap namespace with a private tmpfs /tmp (alloy.sandbox)."""
    branch_from_tests: bool = True
    """`sessions.branch_from_tests`: the implementer and the verifier each run
    in their own branch (fork) of the tests writer's session -- the agent that
    wrote the tests writes the code, with its context and prompt cache, and
    a separate branch of the same session checks it (see
    alloy.recipes.shared_verification.call_on_branch). False: both start cold
    with their own configured runners, as before."""
    graph: str | None = None
    """Registered graph (alloy.recipes.REGISTRY) this recipe runs; when unset,
    the graph is looked up by the recipe's own (file) name. Lets a project add
    a recipe under a new name (`graph: tdd-loop-complex-no-context`) instead of
    shadowing a built-in file."""
    prepare: tuple[CommandSpec, ...] = ()
    """`script` graph only: commands run once, in order, before the checks."""
    checks: tuple[CommandSpec, ...] = ()
    """`script` graph only: checks run one by one, in order, never chained."""
    landing_gate: tuple[LandingGateEntry, ...] = ()
    """Checks a bead must pass on its final tree before it finishes done,
    selected by the paths its diff touches (alloy.recipes.landing_gate).
    Empty: no gate, the run finishes as before."""
    escalate: EscalateSpec | None = None
    """fast-track only: reroute a mis-sized bead to a fuller recipe. None: off."""

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
            raise ConfigError(f"tiered role '{name}' requires a known complexity level, got {complexity!r}")
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
        legacy_verify = raw.get("verify") or {}
        if legacy_verify:
            log.warning(
                "the 'verify:' block is deprecated: Alloy runs the checks the verifier "
                "names, not a fixed command; use 'verification:' for limits"
                + (" (verify.command is ignored)" if legacy_verify.get("command") else "")
            )
        graph = raw.get("graph")
        if graph is not None and (not isinstance(graph, str) or not graph.strip()):
            raise ConfigError(f"recipe 'graph' must be a registered graph name, got {graph!r}")
        sessions = raw.get("sessions") or {}
        if not isinstance(sessions, dict):
            raise ConfigError(f"recipe 'sessions' must be a mapping, got {sessions!r}")
        name = raw.get("name") or (source.stem if source else "unnamed")
        prepare = _parse_commands(raw.get("prepare"), where="prepare")
        checks = _parse_commands(raw.get("checks"), where="checks")
        if (graph.strip() if graph else name) == SCRIPT_GRAPH and not checks:
            raise ConfigError(f"recipe '{name}' runs the script graph but declares no 'checks'")
        landing_gate = _parse_landing_gate(raw.get("landing_gate"))
        if (graph.strip() if graph else name) == SCRIPT_GRAPH and landing_gate:
            raise ConfigError(
                f"recipe '{name}' runs the script graph, which has no landing gate: "
                "put those commands in its 'checks' instead of 'landing_gate'"
            )
        escalate = EscalateSpec.parse(raw.get("escalate"))
        if escalate is not None and escalate.to == name:
            raise ConfigError(f"recipe '{name}' cannot escalate to itself")
        return cls(
            name=name,
            prepare=prepare,
            checks=checks,
            landing_gate=landing_gate,
            escalate=escalate,
            graph=graph.strip() if graph else None,
            branch_from_tests=bool(sessions.get("branch_from_tests", True)),
            roles=roles,
            complexity=complexity,
            consilium=ConsiliumSpec.parse(raw.get("consilium")),
            limits=Limits.parse(raw.get("limits")),
            verification=VerificationSpec.parse(raw.get("verification"), legacy=legacy_verify),
            memory=MemorySpec.parse(raw.get("memory")),
            sandbox=SandboxSpec.parse(raw.get("sandbox")),
            runners=dict(raw.get("runners") or {}),
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


def discover_recipes(alloy_root: Path | None = None, project: Path | None = None) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for directory in recipe_search_path(alloy_root, project):
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob("*.y*ml")):
            found[candidate.stem] = candidate
    return found


def load_recipe(name: str, *, alloy_root: Path | None = None, project: Path | None = None) -> RecipeConfig:
    available = discover_recipes(alloy_root, project)
    path = available.get(name)
    if path is None:
        known = ", ".join(sorted(available)) or "(none)"
        raise ConfigError(f"unknown recipe '{name}'; available: {known}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"recipe file {path} must contain a mapping")
    raw.setdefault("name", name)
    config = RecipeConfig.parse(raw, source=path)
    if config.escalate is not None and config.escalate.to not in available:
        known = ", ".join(sorted(available))
        raise ConfigError(f"recipe '{name}' escalates to unknown recipe '{config.escalate.to}'; available: {known}")
    return config
