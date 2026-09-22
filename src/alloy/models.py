"""Value types exchanged between Alloy's components.

These are deliberately small. Anything large -- full transcripts, complete test
output, diffs -- is written to disk and referenced here by path, so that graph
state stays cheap to checkpoint and cheap to hand to the next agent.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

MAX_EMBEDDED_TEXT = 4000
"""Hard cap on any agent text copied into graph state."""

COMPLEXITY_LEVELS = ("simple", "medium", "complex")
Complexity = Literal["simple", "medium", "complex"]


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


class RunnerUnavailable(RuntimeError):
    """The harness CLI is not installed or not authenticated."""


class ContextPacket(BaseModel):
    """Compact repository understanding produced by the context role."""

    summary: str = ""
    relevant_files: list[str] = Field(default_factory=list)
    test_command: str | None = None
    conventions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)

    def compact(self) -> dict[str, Any]:
        return {
            "summary": clip(self.summary, 1500),
            "relevant_files": self.relevant_files[:25],
            "test_command": self.test_command,
            "conventions": self.conventions[:10],
            "risks": self.risks[:10],
        }


class TestReport(BaseModel):
    """Result of running the deterministic verification command."""

    command: str
    exit_code: int
    passed: int | None = None
    failed: int | None = None
    duration_s: float = 0.0
    tail: str = ""
    log_path: str | None = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def headline(self) -> str:
        if self.timed_out:
            return f"timed out after {self.duration_s:.0f}s"
        if self.passed is None and self.failed is None:
            return "exit 0" if self.exit_code == 0 else f"exit {self.exit_code}"
        return f"{self.passed or 0} passed, {self.failed or 0} failed"


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
    tests: str = ""
    decision: str = ""
    reason: str = ""

    def render(self) -> str:
        return (
            f"#{self.iteration} via {self.implementer}: {clip(self.change_summary, 300)}\n"
            f"   tests: {self.tests}\n"
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
