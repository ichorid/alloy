"""Value types exchanged between Alloy's components.

These are deliberately small. Anything large -- full transcripts, complete test
output, diffs -- is written to disk and referenced here by path, so that graph
state stays cheap to checkpoint and cheap to hand to the next agent.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator

if TYPE_CHECKING:  # pragma: no cover - config imports models; avoid the cycle at runtime
    from alloy.config import MemorySpec

MAX_EMBEDDED_TEXT = 4000
"""Hard cap on any agent text copied into graph state."""

COMPLEXITY_LEVELS = ("simple", "medium", "complex")
Complexity = Literal["simple", "medium", "complex"]


class ComplexityEstimate(BaseModel):
    complexity: Complexity
    reason: str = ""
    confidence: float = 0.0

    @classmethod
    def schema_for_agents(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "complexity": {"type": "string", "enum": list(COMPLEXITY_LEVELS)},
                "reason": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["complexity", "reason", "confidence"],
            "additionalProperties": False,
        }


def next_level(level: Complexity) -> str:
    index = COMPLEXITY_LEVELS.index(level)
    return COMPLEXITY_LEVELS[min(index + 1, len(COMPLEXITY_LEVELS) - 1)]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def clip(text: str, limit: int = MAX_EMBEDDED_TEXT) -> str:
    """Keep head and tail; the middle is the least informative part of agent output."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    dropped = len(text) - limit
    return f"{text[:head]}\n\n...[{dropped} chars elided; see log]...\n\n{text[-tail:]}"


class AgentResult(BaseModel):
    """Normalized outcome of one harness invocation."""

    runner: str
    model: str | None = None
    ok: bool
    exit_code: int
    text: str = ""
    structured: dict[str, Any] | None = None
    started_at: datetime
    ended_at: datetime
    duration_s: float
    usage: dict[str, Any] = Field(default_factory=dict)
    log_path: str | None = None
    prompt_hash: str = ""
    prefix_hash: str = ""
    """sha256 of the prompt's static, project and run layers (see alloy.prompts)."""
    error: str | None = None
    session_id: str | None = None
    retry_at: datetime | None = None
    """When a failed harness said it will be available again (see alloy.limits)."""

    @property
    def summary(self) -> str:
        if self.ok:
            return clip(self.text, 600)
        return f"[{self.runner} failed exit={self.exit_code}] {self.error or clip(self.text, 400)}"


class BugReport(BaseModel):
    title: str
    where: str = ""
    evidence: str = ""
    blocks_task: bool | None = None
    reporter: str = ""
    iteration: int = 0


def extract_bug_reports(text: str) -> list[BugReport]:
    """Read optional bug blocks without rejecting incomplete role output."""
    reports = []
    titles = set()
    for block in re.findall(r"<bug>(.*?)</bug>", text, re.DOTALL):
        fields = {}
        for line in block.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                fields[key.strip().lower()] = value.strip()
        title = fields.get("title", "")
        if not title or title in titles:
            continue
        titles.add(title)
        reports.append(BugReport(
            title=title,
            where=fields.get("where", ""),
            evidence=fields.get("evidence", ""),
            blocks_task={"yes": True, "true": True, "no": False, "false": False}.get(
                fields.get("blocks_task", "").lower()
            ),
        ))
    return reports


BUG_SEVERITIES: tuple[str, ...] = (
    "not-a-bug", "duplicate", "non-blocking", "blocking", "needs-human",
)
BugSeverity = Literal["not-a-bug", "duplicate", "non-blocking", "blocking", "needs-human"]


class BugTriage(BaseModel):
    """The triage role's verdict on one `<bug>` report."""

    severity: BugSeverity
    reason: str = ""
    confidence: float = 0.0

    @classmethod
    def schema_for_agents(cls) -> dict[str, Any]:
        # `severity` first: Jev classifies on the first enum-valued property.
        return {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": list(BUG_SEVERITIES)},
                "reason": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["severity", "reason", "confidence"],
            "additionalProperties": False,
        }


SCOPE_VERDICTS: tuple[str, ...] = ("merge", "too-broad", "subverts-task")
ScopeLabel = Literal["merge", "too-broad", "subverts-task"]


class ScopeVerdict(BaseModel):
    """The scope role's answer: may a remediation child's diff merge into its parent?"""

    verdict: ScopeLabel
    reason: str = ""
    confidence: float = 0.0

    @classmethod
    def schema_for_agents(cls) -> dict[str, Any]:
        # `verdict` first: Jev classifies on the first enum-valued property.
        return {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": list(SCOPE_VERDICTS)},
                "reason": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["verdict", "reason", "confidence"],
            "additionalProperties": False,
        }


ACCEPTANCE_DECISIONS: tuple[str, ...] = ("accept", "verify_more", "repair", "escalate")
AcceptanceDecision = Literal["accept", "verify_more", "repair", "escalate"]


class AcceptanceVerdict(BaseModel):
    """The acceptance role's answer: is there enough evidence to call the task complete?"""

    decision: AcceptanceDecision
    reason: str = ""
    confidence: float = 0.0

    @classmethod
    def schema_for_agents(cls) -> dict[str, Any]:
        # `decision` first: Jev classifies on the first enum-valued property.
        return {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": list(ACCEPTANCE_DECISIONS)},
                "reason": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["decision", "reason", "confidence"],
            "additionalProperties": False,
        }


class ProjectSnapshot(BaseModel):
    """The bead-graph half of the project context packet, already rendered
    one line per bead. Every section is empty when `bd` could not answer."""

    brief_source: str = ""
    open_beads: list[str] = Field(default_factory=list)
    epic: str = ""
    filed_bugs: list[str] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)


class RunnerUnavailable(RuntimeError):
    """The harness CLI is not installed or not authenticated."""


class ContextPacket(BaseModel):
    """Compact repository understanding produced by the context role."""

    summary: str = ""
    relevant_files: list[str] = Field(default_factory=list)
    check_hints: list[str] = Field(default_factory=list)
    """Commands the repository suggests for tests, lint or build. Hints for the
    verifier, never something Alloy runs on its own."""
    conventions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    memory_contradictions: list[str] = Field(default_factory=list)
    """Project memories the repository contradicts, each ``key: why``. Flagged
    for a reviewer under alloy:review:contradiction:<key>; the disputed memory
    itself is never touched."""

    def compact(self) -> dict[str, Any]:
        return {
            "summary": clip(self.summary, 1500),
            "relevant_files": self.relevant_files[:25],
            "check_hints": self.check_hints[:10],
            "conventions": self.conventions[:10],
            "risks": self.risks[:10],
            "memory_contradictions": self.memory_contradictions[:5],
        }


CHECK_KINDS: tuple[str, ...] = ("regression", "targeted", "lint", "typecheck", "build", "custom")


class CheckRequest(BaseModel):
    """What to run and why. Alloy executes it; it never interprets it."""

    command: str
    purpose: str = ""
    kind: str = "custom"
    required: bool = True

    @field_validator("kind", mode="before")
    @classmethod
    def _known_kind(cls, value: Any) -> str:
        return value if value in CHECK_KINDS else "custom"


class CheckResult(BaseModel):
    """What happened when a check ran: the exit code is the fact."""

    command: str
    purpose: str = ""
    kind: str = "custom"
    required: bool = True
    iteration: int = 0
    """The implement iteration this check verified; 0 for the baseline."""
    exit_code: int
    duration_s: float = 0.0
    timed_out: bool = False
    output_tail: str = Field(default="", validation_alias=AliasChoices("output_tail", "tail"))
    log_path: str | None = None
    passed: int | None = None
    failed: int | None = None

    @property
    def tail(self) -> str:  # legacy name for output_tail
        return self.output_tail

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def runnable(self) -> bool:
        return not self.timed_out and self.exit_code != 127

    def headline(self) -> str:
        if self.timed_out:
            # No duration: headlines land in prompts, where a clock value
            # would differ between otherwise identical renderings.
            return "timed out"
        if self.exit_code == 127:
            return "command not found"
        if self.passed is None and self.failed is None:
            return f"exit {self.exit_code}"
        return f"{self.passed or 0} passed, {self.failed or 0} failed"


VERIFIER_ACTIONS: tuple[str, ...] = ("run", "stop")


class VerifierAction(BaseModel):
    """The verifier role's answer: one more check to run, or stop.

    Alloy executes `command` as-is in the worktree root; a `run` without a
    command is invalid and is treated as a verifier failure by the recipe."""

    action: Literal["run", "stop"]
    command: str = ""
    purpose: str = ""
    kind: str = "custom"
    required: bool = True
    reason: str = ""
    remaining_risks: list[str] = Field(default_factory=list)

    @field_validator("kind", mode="before")
    @classmethod
    def _known_kind(cls, value: Any) -> str:
        return value if value in CHECK_KINDS else "custom"

    def to_request(self) -> CheckRequest:
        return CheckRequest(
            command=self.command.strip(), purpose=self.purpose, kind=self.kind,
            required=self.required,
        )

    @classmethod
    def schema_for_agents(cls) -> dict[str, Any]:
        # `action` first: Jev classifies on the first enum-valued property.
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(VERIFIER_ACTIONS)},
                "command": {"type": "string"},
                "purpose": {"type": "string"},
                "kind": {"type": "string", "enum": list(CHECK_KINDS)},
                "required": {"type": "boolean"},
                "reason": {"type": "string"},
                "remaining_risks": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "action", "command", "purpose", "kind", "required", "reason", "remaining_risks",
            ],
            "additionalProperties": False,
        }


class TestsOutput(BaseModel):
    """Structured answer of the tests role: what it wrote and the exact commands
    that prove the requested behaviour is not implemented yet. Alloy runs them;
    the role never says whether they passed."""

    summary: str = ""
    baseline_checks: list[CheckRequest] = Field(default_factory=list)

    @field_validator("baseline_checks", mode="after")
    @classmethod
    def _targeted_and_required(cls, checks: list[CheckRequest]) -> list[CheckRequest]:
        return [
            check.model_copy(update={"kind": "targeted", "required": True}) for check in checks
        ]

    @classmethod
    def schema_for_agents(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "baseline_checks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "purpose": {"type": "string"},
                        },
                        "required": ["command", "purpose"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["summary", "baseline_checks"],
            "additionalProperties": False,
        }


Decision = Literal["done", "retry", "consilium", "human", "abort"]
DECISIONS: tuple[str, ...] = ("done", "retry", "consilium", "human", "abort")


class JudgeDecision(BaseModel):
    """Structured verdict required from the judge role."""

    decision: Decision
    reason: str = ""
    next_instructions: str = ""
    confidence: float = 0.0
    retry_at: str | None = None
    """Alloy-internal: ISO time a parked run may resume on its own. Never part
    of the schema agents answer with."""

    @classmethod
    def schema_for_agents(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": list(DECISIONS)},
                "reason": {"type": "string"},
                "next_instructions": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["decision", "reason", "next_instructions", "confidence"],
            "additionalProperties": False,
        }


class Critique(BaseModel):
    """One consilium critic's independent opinion. Critics never touch code."""

    critic: str
    root_cause: str = ""
    evidence: str = ""
    suggested_fix: str = ""
    confidence: float = 0.0
    failed: bool = False

    @classmethod
    def schema_for_agents(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "root_cause": {"type": "string"},
                "evidence": {"type": "string"},
                "suggested_fix": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["root_cause", "evidence", "suggested_fix", "confidence"],
            "additionalProperties": False,
        }


class Attempt(BaseModel):
    """One compact row of history carried between iterations."""

    iteration: int
    implementer: str
    change_summary: str = ""
    checks: str = ""
    decision: str = ""
    reason: str = ""
    changed_tests: list[str] = Field(default_factory=list)

    def render(self) -> str:
        edited = (
            f"   tests edited: {', '.join(self.changed_tests)}\n" if self.changed_tests else ""
        )
        return (
            f"#{self.iteration} via {self.implementer}: {clip(self.change_summary, 300)}\n"
            f"   checks: {self.checks}\n"
            f"{edited}"
            f"   judge: {self.decision} -- {clip(self.reason, 200)}"
        )


class Outcome(str, Enum):
    DONE = "done"
    FAILED = "failed"
    WAITING_HUMAN = "waiting-human"
    ABORTED = "aborted"
    CANCELLED = "cancelled"


class AgentCallRecord(BaseModel):
    """Ledger row -- the unit Alloy later compares recipes and harnesses on."""

    run_id: str
    bead_id: str
    role: str
    runner: str
    model: str | None
    prompt_hash: str
    prefix_hash: str
    started_at: datetime
    ended_at: datetime
    duration_s: float
    exit_code: int
    ok: bool
    usage_json: str
    log_path: str | None
    iteration: int


# ---------------------------------------------------------------------------
# Project memory (alloy-4ef.3)
# ---------------------------------------------------------------------------

MEMORY_OWNER_PREFIX = "alloy:"
"""Keys with this prefix are alloy-owned; every other key is human-owned."""

MEMORY_RENDER_HEADER = "## Project memory"
REGRESSION_KEY_PREFIX = "alloy:regression:"

_MEMORY_EXCLUDED_PREFIXES = ("alloy:meta:", "alloy:review:", REGRESSION_KEY_PREFIX)
_MEMORY_EXCLUDED_KEYS = frozenset({"alloy:calibration"})
_MEMORY_LESSON_PREFIX = "alloy:lesson"

_PROVENANCE_RE = re.compile(
    r"(?P<body>.*) \[alloy run=(?P<run>\S+) bead=(?P<bead>\S+) at=(?P<at>\d{4}-\d{2}-\d{2})\]",
    re.DOTALL,
)


def with_provenance(text: str, run_id: str, bead_id: str, at: date) -> str:
    """Append the provenance trailer writers stamp on alloy-owned memories."""

    return f"{text} [alloy run={run_id} bead={bead_id} at={at.isoformat()}]"


def parse_provenance(content: str) -> tuple[str, str | None, str | None, date | None]:
    """Split ``content`` into (body, run_id, bead_id, date). Only a trailer at
    the very end counts; anything else is left in the body untouched."""

    match = _PROVENANCE_RE.fullmatch(content)
    if match is None:
        return content, None, None, None
    try:
        at = date.fromisoformat(match.group("at"))
    except ValueError:
        return content, None, None, None
    return match.group("body"), match.group("run"), match.group("bead"), at


def parse_bug_where(description: str) -> str:
    """Read the location line written by bug_description()."""
    match = re.search(r"^where:[ \t]*(.*)$", description, re.MULTILINE)
    return match.group(1).strip() if match else ""


def regression_key(where: str) -> str | None:
    """Map a repository-relative bug location to its top-level memory key."""
    parts = PurePosixPath(where.strip().split(":", 1)[0]).parts
    if not parts or parts[0] in {"/", "..", "(not given)"}:
        return None
    return f"{REGRESSION_KEY_PREFIX}{parts[0]}"


def format_regression_areas(memories: dict[str, str], relevant_files: list[str]) -> str:
    """Render only regression memories matching the context's path prefixes."""
    keys = {regression_key(path) for path in relevant_files}
    rows = [
        f"### {key}\n{parse_provenance(body)[0]}"
        for key, body in sorted(memories.items())
        if key in keys
    ]
    return "## Known regression areas\n\n" + "\n\n".join(rows) if rows else ""


@dataclass(frozen=True)
class MemoryEntry:
    """One project memory: who owns it, its prompt-visible body and, for
    alloy-owned entries, the provenance parsed off the trailer."""

    key: str
    body: str
    owner: Literal["alloy", "human"]
    run_id: str | None = None
    bead_id: str | None = None
    date: date | None = None

    @property
    def included(self) -> bool:
        """Whether this entry may appear in the prompt block at all."""

        return not (
            self.key in _MEMORY_EXCLUDED_KEYS or self.key.startswith(_MEMORY_EXCLUDED_PREFIXES)
        )

    @property
    def cap_group(self) -> int:
        """Cap priority: human first, then alloy:lesson*, then other alloy keys."""

        if self.owner == "human":
            return 0
        if self.key.startswith(_MEMORY_LESSON_PREFIX):
            return 1
        return 2


@dataclass(frozen=True)
class ProjectMemory:
    """Project memories parsed from ``bd memories`` plus the recipe's caps.

    ``render()`` is a pure function of (memories, spec): input order never
    matters and the block carries no dates, ids or counts.
    """

    entries: dict[str, MemoryEntry]
    max_items: int
    max_chars: int
    _rendered: str = field(init=False, repr=False, compare=False, default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "_rendered", self._render())

    @classmethod
    def from_raw(cls, memories: dict[str, str], spec: MemorySpec) -> ProjectMemory:
        entries: dict[str, MemoryEntry] = {}
        for key in sorted(memories):
            content = memories[key]
            if key.startswith(MEMORY_OWNER_PREFIX):
                body, run_id, bead_id, at = parse_provenance(content)
                entries[key] = MemoryEntry(key, body, "alloy", run_id, bead_id, at)
            else:
                entries[key] = MemoryEntry(key, content, "human")
        return cls(entries=entries, max_items=spec.max_items, max_chars=spec.max_chars)

    def selected(self) -> list[MemoryEntry]:
        """Entries that survive the caps, in cap priority order."""

        candidates = sorted(
            (entry for entry in self.entries.values() if entry.included),
            key=lambda entry: (entry.cap_group, entry.key),
        )[: max(self.max_items, 0)]
        chosen: list[MemoryEntry] = []
        for entry in candidates:
            if len(_render_block(chosen + [entry])) > self.max_chars:
                break
            chosen.append(entry)
        return chosen

    def _render(self) -> str:
        return _render_block(self.selected())

    def render(self) -> str:
        """Fixed-format prompt block sorted by key; empty when nothing fits."""

        return self._rendered

    def body_of(self, key: str) -> str:
        """The provenance-stripped body stored under ``key``; "" when absent."""

        entry = self.entries.get(key)
        return entry.body if entry is not None else ""


def _render_block(entries: list[MemoryEntry]) -> str:
    if not entries:
        return ""
    lines = [MEMORY_RENDER_HEADER, ""]
    for entry in sorted(entries, key=lambda entry: entry.key):
        lines.extend((f"### {entry.key}", entry.body, ""))
    return "\n".join(lines).rstrip("\n")


# ---------------------------------------------------------------------------
# alloy:check-hints (alloy-4ef.9)
# ---------------------------------------------------------------------------

CHECK_HINTS_KEY = "alloy:check-hints"
CONTRADICTION_KEY_PREFIX = "alloy:review:contradiction:"
"""The verifier's runnable check commands from the last DONE run on this repo."""

# ---------------------------------------------------------------------------
# alloy memory list (alloy-4ef.15)
# ---------------------------------------------------------------------------

EMBED_KEY = "alloy:meta:embed"
"""Meta key listing the memory keys already embedded into instruction files."""

PROPOSAL_KEY_PREFIX = "alloy:review:proposal:"
"""Meta prefix under which a pending review proposal for <key> is stored."""

_META_KEY_PREFIXES = ("alloy:meta:", "alloy:review:")

FLAG_CONTRADICTION = "contradiction"
FLAG_EMBEDDED = "embedded"
FLAG_PROPOSAL = "proposal"


def _embedded_keys(body: str) -> set[str]:
    """Keys named by ``alloy:meta:embed``: a JSON list, or whitespace/comma
    separated text."""

    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, list):
        return {str(item) for item in data}
    return {token for token in re.split(r"[\s,]+", body) if token}


def memory_inventory(memory: ProjectMemory, today: date) -> list[dict[str, Any]]:
    """One row per non-meta memory: key, owner, provenance, age in days and
    the flags derived from the ``alloy:meta:*`` / ``alloy:review:*`` keys."""

    entries = memory.entries
    embedded = _embedded_keys(entries[EMBED_KEY].body) if EMBED_KEY in entries else set()
    rows: list[dict[str, Any]] = []
    for key in sorted(entries):
        if key.startswith(_META_KEY_PREFIXES):
            continue
        entry = entries[key]
        flags: list[str] = []
        if CONTRADICTION_KEY_PREFIX + key in entries:
            flags.append(FLAG_CONTRADICTION)
        if key in embedded:
            flags.append(FLAG_EMBEDDED)
        if PROPOSAL_KEY_PREFIX + key in entries:
            flags.append(FLAG_PROPOSAL)
        rows.append({
            "key": key,
            "owner": entry.owner,
            "run_id": entry.run_id,
            "bead_id": entry.bead_id,
            "date": entry.date.isoformat() if entry.date else None,
            "age_days": (today - entry.date).days if entry.date else None,
            "flags": flags,
        })
    return rows

# ---------------------------------------------------------------------------
# alloy memory review (alloy-4ef.16)
# ---------------------------------------------------------------------------

REVIEW_ACTIONS: tuple[str, ...] = ("keep", "update", "forget", "embed")
ReviewAction = Literal["keep", "update", "forget", "embed"]

REVIEW_SOURCES: tuple[str, ...] = ("hygiene", "reviewer")
ReviewSource = Literal["hygiene", "reviewer"]


class ReviewVerdict(BaseModel):
    """One memory_reviewer verdict: what to do with the memory stored under ``key``."""

    action: ReviewAction
    key: str
    reason: str = ""
    new_content: str | None = None


class ReviewVerdicts(BaseModel):
    """The memory_reviewer role's answer: one verdict per reviewed key."""

    verdicts: list[ReviewVerdict]
    reason: str = ""

    @classmethod
    def schema_for_agents(cls) -> dict[str, Any]:
        # `action` first inside each item: Jev classifies on the first enum-valued property.
        return {
            "type": "object",
            "properties": {
                "verdicts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": list(REVIEW_ACTIONS)},
                            "key": {"type": "string"},
                            "reason": {"type": "string"},
                            "new_content": {"type": "string"},
                        },
                        "required": ["action", "key", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["verdicts"],
            "additionalProperties": False,
        }


class ReviewPlanItem(BaseModel):
    """One planned change to project memory and where the plan got it from."""

    key: str
    action: ReviewAction
    reason: str
    source: ReviewSource
    new_content: str | None = None


class ReviewPlan(BaseModel):
    """What ``alloy memory review`` would do; nothing is written without --apply."""

    items: list[ReviewPlanItem] = Field(default_factory=list)
    reviewer_ok: bool = False
    reviewer_reason: str = ""

    def keys(self) -> set[str]:
        return {item.key for item in self.items}


def memory_hygiene(memory: ProjectMemory, ttl_days: int, today: date) -> list[ReviewPlanItem]:
    """The deterministic review items, one per key, in key order:

    - an alloy-owned memory whose provenance date is older than ``ttl_days``
      is forgotten;
    - an ``alloy:review:contradiction:<key>`` flag whose subject is gone is
      forgotten;
    - of two or more keys with byte-identical (provenance-stripped) bodies,
      the oldest is kept and the newer ones are forgotten. Undated entries
      count as oldest; equal dates tie-break on key order.
    """

    entries = memory.entries
    items: dict[str, ReviewPlanItem] = {}

    def forget(key: str, reason: str) -> None:
        items.setdefault(key, ReviewPlanItem(key=key, action="forget", reason=reason,
                                             source="hygiene"))

    for key, entry in entries.items():
        if entry.owner == "alloy" and entry.date is not None:
            age = (today - entry.date).days
            if age > ttl_days:
                forget(key, f"alloy-owned memory is {age} days old (ttl {ttl_days})")
        if key.startswith(CONTRADICTION_KEY_PREFIX):
            subject = key[len(CONTRADICTION_KEY_PREFIX):]
            if subject not in entries:
                forget(key, f"contradiction flag for missing key {subject!r}")

    by_body: dict[str, list[MemoryEntry]] = {}
    for entry in entries.values():
        if entry.body.strip():
            by_body.setdefault(entry.body, []).append(entry)
    for group in by_body.values():
        if len(group) < 2:
            continue
        ordered = sorted(
            group, key=lambda entry: (entry.date is not None, entry.date or date.min, entry.key)
        )
        keeper = ordered[0]
        for entry in ordered[1:]:
            forget(entry.key, f"byte-identical to older key {keeper.key!r}")

    return [items[key] for key in sorted(items)]


def merge_review_plan(
    hygiene: list[ReviewPlanItem],
    verdicts: ReviewVerdicts | None,
    known_keys: set[str],
    *,
    reviewer_reason: str = "",
) -> ReviewPlan:
    """Hygiene items first, then the reviewer's verdicts on keys the snapshot
    knows and hygiene has not already decided. ``verdicts`` is None when the
    reviewer failed or answered malformed: the plan is then hygiene only."""

    items = list(hygiene)
    taken = {item.key for item in items}
    if verdicts is not None:
        for verdict in verdicts.verdicts:
            if verdict.key not in known_keys or verdict.key in taken:
                continue
            taken.add(verdict.key)
            items.append(ReviewPlanItem(
                key=verdict.key, action=verdict.action, reason=verdict.reason,
                source="reviewer", new_content=verdict.new_content,
            ))
    return ReviewPlan(items=items, reviewer_ok=verdicts is not None,
                      reviewer_reason=reviewer_reason)


# ---------------------------------------------------------------------------
# alloy memory review --apply (alloy-4ef.17)
# ---------------------------------------------------------------------------

LAST_REVIEW_KEY = "alloy:meta:last-review"
"""Meta key holding the ISO date of the last applied memory review."""

MEMORY_REVIEW_LABEL = "alloy-memory-review"
"""Label on the task bead that lists pending proposals on human-owned memories."""

EMBED_STALE_KEY = "alloy:meta:embed-stale"
"""Meta flag set when the embedded block may lag the memory set; the scheduler
treats it as a due trigger for review + embed and clears it once applied."""

_EMBED_EXCLUDED_PREFIXES = _META_KEY_PREFIXES
_EMBED_EXCLUDED_KEYS = frozenset({"alloy:calibration"})


class ReviewApply(BaseModel):
    """The bd writes an applied review plan comes down to, in execution order.

    Alloy-owned ``forget``/``update`` verdicts become ``forgets`` and
    ``remembers``; ``forget``/``update`` on human-owned keys are never
    executed and become ``proposals`` (each also a remember under
    ``alloy:review:proposal:<key>``). ``embed_keys`` is what
    ``alloy:meta:embed`` is set to; ``alloy:meta:last-review`` is always
    written.
    """

    forgets: list[str] = Field(default_factory=list)
    remembers: list[tuple[str, str]] = Field(default_factory=list)
    proposals: list[str] = Field(default_factory=list)
    embed_keys: list[str] = Field(default_factory=list)


def _embeddable(key: str) -> bool:
    return not (key in _EMBED_EXCLUDED_KEYS or key.startswith(_EMBED_EXCLUDED_PREFIXES))


def proposal_body(item: ReviewPlanItem) -> str:
    """The JSON stored under ``alloy:review:proposal:<key>``: the verdict, its
    reason and, for updates, the proposed content."""

    payload: dict[str, str] = {"action": item.action, "reason": item.reason}
    if item.new_content is not None:
        payload["new_content"] = item.new_content
    return json.dumps(payload)


def plan_review_apply(plan: ReviewPlan, *, run_id: str, bead_id: str, today: date) -> ReviewApply:
    """Turn a review plan into bd writes. Pure: nothing is executed here.

    Ownership is by key prefix (``alloy:`` is alloy-owned). An alloy-owned
    ``update`` without ``new_content`` is skipped; ``keep`` and ``embed``
    never forget anything.
    """

    apply = ReviewApply()
    embed: set[str] = set()
    for item in plan.items:
        alloy_owned = item.key.startswith(MEMORY_OWNER_PREFIX)
        if item.action == "embed":
            if _embeddable(item.key):
                embed.add(item.key)
            continue
        if item.action == "keep":
            continue
        if not alloy_owned:
            apply.proposals.append(item.key)
            apply.remembers.append((PROPOSAL_KEY_PREFIX + item.key, proposal_body(item)))
            continue
        if item.action == "forget":
            apply.forgets.append(item.key)
        elif item.new_content is not None:
            apply.remembers.append(
                (item.key, with_provenance(item.new_content, run_id, bead_id, today))
            )
    apply.embed_keys = sorted(embed)
    apply.remembers.append((EMBED_KEY, json.dumps(apply.embed_keys)))
    apply.remembers.append((LAST_REVIEW_KEY, today.isoformat()))
    return apply


def review_bead_text(proposals: list[str]) -> tuple[str, str]:
    """Title and description of the task bead listing pending proposals."""

    title = f"Review {len(proposals)} memory proposals"
    lines = [
        "Alloy proposed changes to these human-owned project memories. Each "
        f"proposal is stored under {PROPOSAL_KEY_PREFIX}<key>; apply or "
        "dismiss it with bd remember/forget.",
        "",
        *(f"- {key}" for key in proposals),
    ]
    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# alloy:calibration (alloy-4ef.12)
# ---------------------------------------------------------------------------

CALIBRATION_KEY = "alloy:calibration"
"""Per-complexity-level aggregate of finished runs, read by the estimate role only."""


def _load_calibration(body: str) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(body) if body.strip() else {}
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        level: entry
        for level, entry in data.items()
        if level in COMPLEXITY_LEVELS and isinstance(entry, dict)
    }


def update_calibration(
    previous_json: str, level: str, iterations: int, agent_calls: int, overrun: bool
) -> str:
    """Fold one finished run into the stored alloy:calibration JSON: per level
    ``runs``, ``mean_iterations``, ``mean_agent_calls`` (one decimal) and
    ``overruns`` (runs that hit a limit). An unreadable previous body starts over."""

    data = _load_calibration(previous_json)
    entry = data.get(level) or {}
    runs = int(entry.get("runs", 0) or 0)

    def running_mean(key: str, value: float) -> float:
        previous = float(entry.get(key, 0.0) or 0.0)
        return round((previous * runs + value) / (runs + 1), 1)

    data[level] = {
        "runs": runs + 1,
        "mean_iterations": running_mean("mean_iterations", iterations),
        "mean_agent_calls": running_mean("mean_agent_calls", agent_calls),
        "overruns": int(entry.get("overruns", 0) or 0) + (1 if overrun else 0),
    }
    ordered = {level: data[level] for level in COMPLEXITY_LEVELS if level in data}
    return json.dumps(ordered, separators=(",", ":"), sort_keys=False)


def format_calibration(body: str) -> str:
    """One deterministic ``calibration: <level> N runs avg X.X it avg Y.Y calls
    Z overruns, ...`` line in level order; empty when nothing is stored."""

    data = _load_calibration(body)
    parts = []
    for level in COMPLEXITY_LEVELS:
        entry = data.get(level)
        if not entry:
            continue
        parts.append(
            f"{level} {int(entry.get('runs', 0) or 0)} runs"
            f" avg {float(entry.get('mean_iterations', 0.0) or 0.0):.1f} it"
            f" avg {float(entry.get('mean_agent_calls', 0.0) or 0.0):.1f} calls"
            f" {int(entry.get('overruns', 0) or 0)} overruns"
        )
    return f"calibration: {', '.join(parts)}" if parts else ""

# ---------------------------------------------------------------------------
# alloy:lesson:<key> (alloy-4ef.10)
# ---------------------------------------------------------------------------

LESSON_KEY_PREFIX = f"{_MEMORY_LESSON_PREFIX}:"
"""Namespace of the repo-level lessons the harvest role writes."""

HARVEST_SCOPES: tuple[str, ...] = ("repo", "task", "none")
HarvestScope = Literal["repo", "task", "none"]


class HarvestAnswer(BaseModel):
    """The harvest role's answer: is there a lesson worth keeping, and for whom?"""

    scope: HarvestScope
    key: str = ""
    lesson: str = ""
    confidence: float = 0.0
    reason: str = ""

    @classmethod
    def schema_for_agents(cls) -> dict[str, Any]:
        # `scope` first: Jev classifies on the first enum-valued property.
        return {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": list(HARVEST_SCOPES)},
                "key": {"type": "string"},
                "lesson": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["scope", "key", "lesson", "confidence"],
            "additionalProperties": False,
        }

    def memory_key(self) -> str:
        """The full alloy:lesson:<key> memory key, or "" when no key was named.
        A key that already carries the namespace (an existing lesson) is kept."""

        key = self.key.strip().removeprefix(LESSON_KEY_PREFIX).strip()
        return f"{LESSON_KEY_PREFIX}{key}" if key else ""

MAX_CHECK_HINTS = 8


def format_check_hints(checks: list[CheckResult]) -> str:
    """One ``<kind>: <command>`` line per distinct runnable check, most recent
    first (a command that ran twice keeps its latest kind), capped at
    MAX_CHECK_HINTS. Empty when nothing was runnable."""

    lines: list[str] = []
    seen: set[str] = set()
    for check in reversed(checks):
        command = check.command.strip()
        if not check.runnable or not command or command in seen:
            continue
        seen.add(command)
        lines.append(f"{check.kind}: {command}")
        if len(lines) == MAX_CHECK_HINTS:
            break
    return "\n".join(lines)


def parse_check_hints(body: str) -> list[str]:
    """The commands of a stored alloy:check-hints body, in stored order."""

    commands: list[str] = []
    for line in body.splitlines():
        kind, sep, command = line.partition(":")
        command = command.strip()
        if sep and kind.strip() in CHECK_KINDS and command and command not in commands:
            commands.append(command)
    return commands
