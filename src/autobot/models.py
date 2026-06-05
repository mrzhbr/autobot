from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class IssueState(StrEnum):
    SEEN = "seen"
    TRIAGED = "triaged"
    NEEDS_SPEC = "needs_spec"
    ASKED = "asked"
    WAITING = "waiting"
    RESUMED = "resumed"
    SPEC_READY = "spec_ready"
    IMPLEMENTING = "implementing"
    REVIEW_LOOP = "review_loop"
    PR_OPEN = "pr_open"
    DONE = "done"
    ABANDONED = "abandoned"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class IssueComment:
    id: int
    author: str
    body: str
    created_at: str


@dataclass(frozen=True)
class Issue:
    repo: str
    number: int
    title: str
    body: str
    author: str
    labels: list[str]
    comments: list[IssueComment] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass(frozen=True)
class ContextFile:
    path: str
    content: str


@dataclass(frozen=True)
class Usage:
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    dollars: float | None = None


@dataclass(frozen=True)
class TriageDecision:
    ready: bool
    questions: list[str]
    reason: str
    usage: Usage | None = None


@dataclass(frozen=True)
class FileChange:
    path: str
    content: str | None
    action: str = "write"


@dataclass(frozen=True)
class ImplementationPlan:
    plan: list[str]
    changes: list[FileChange]
    test_commands: list[str]
    usage: Usage | None = None


@dataclass(frozen=True)
class ReviewFinding:
    severity: str
    file: str
    line: int | None
    message: str
    blocking: bool


@dataclass(frozen=True)
class ReviewReport:
    lens: str
    findings: list[ReviewFinding]
    usage: Usage | None = None


@dataclass(frozen=True)
class ProcessResult:
    state: IssueState
    message: str
    pr_url: str | None
    cost: dict
    branch: str | None
    review_rounds: int
    files_touched: list[str]
    blocked_on: str | None


@dataclass
class IssueRecord:
    repo: str
    issue_number: int
    state: IssueState = IssueState.SEEN
    conversation: dict[str, Any] = field(default_factory=dict)
    branch: str | None = None
    plan: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    blocked_on: str | None = None
    review_rounds: int = 0
    files_touched: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.issue_number}"

    def transition(self, state: IssueState) -> None:
        self.state = state
        self.updated_at = utc_now()
