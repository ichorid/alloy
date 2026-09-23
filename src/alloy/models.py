"""Value types exchanged between Alloy's components.

These are deliberately small. Anything large -- full transcripts, complete test
output, diffs -- is written to disk and referenced here by path, so that graph
state stays cheap to checkpoint and cheap to hand to the next agent.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator

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
    error: str | None = None
    session_id: str | None = None

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

    def compact(self) -> dict[str, Any]:
        return {
            "summary": clip(self.summary, 1500),
            "relevant_files": self.relevant_files[:25],
            "check_hints": self.check_hints[:10],
            "conventions": self.conventions[:10],
            "risks": self.risks[:10],
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
            return f"timed out after {self.duration_s:.0f}s"
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
    started_at: datetime
    ended_at: datetime
    duration_s: float
    exit_code: int
    ok: bool
    usage_json: str
    log_path: str | None
    iteration: int
