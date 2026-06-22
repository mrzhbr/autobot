from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from autobot.scanner import find_secret_like_values


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class TriagePayload(StrictPayload):
    ready: bool
    questions: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_question_when_blocked(self) -> TriagePayload:
        if not self.ready and not self.questions:
            raise ValueError("triage must include at least one question when not ready")
        return self

    @field_validator("questions")
    @classmethod
    def strip_questions(cls, questions: list[str]) -> list[str]:
        return [question.strip() for question in questions if question.strip()][:3]


class FileChangePayload(StrictPayload):
    path: str = Field(min_length=1)
    action: Literal["write", "delete"] = "write"
    content: str | None = None

    @model_validator(mode="after")
    def validate_change(self) -> FileChangePayload:
        if self.path.startswith("/") or ".." in self.path.split("/"):
            raise ValueError("change path must be relative and stay inside the repo")
        if self.action == "write" and self.content is None:
            raise ValueError("write changes require content")
        return self


class ImplementationPayload(StrictPayload):
    plan: list[str] = Field(min_length=1)
    changes: list[FileChangePayload] = Field(min_length=1)
    test_commands: list[str] = Field(default_factory=list)

    @field_validator("plan", "test_commands")
    @classmethod
    def strip_strings(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values if value.strip()]

    @field_validator("test_commands")
    @classmethod
    def reject_secret_like_commands(cls, commands: list[str]) -> list[str]:
        if secrets := find_secret_like_values("\n".join(commands)):
            count = len(secrets)
            raise ValueError(f"secret-like values found in test commands: {count} finding(s)")
        return commands

    @model_validator(mode="after")
    def require_plan_steps(self) -> ImplementationPayload:
        if not self.plan:
            raise ValueError("implementation plan must include at least one non-empty step")
        return self


class PlannerPayload(StrictPayload):
    contract_version: Literal[1]
    summary: str = Field(min_length=1)
    target_files: list[str]
    constraints: list[str]
    implementation_steps: list[str] = Field(min_length=1)
    tests_to_add: list[str]
    verification_commands: list[str]
    risks: list[str]
    non_goals: list[str]

    @field_validator(
        "target_files",
        "constraints",
        "implementation_steps",
        "tests_to_add",
        "verification_commands",
        "risks",
        "non_goals",
    )
    @classmethod
    def strip_strings(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values if value.strip()]

    @field_validator("summary")
    @classmethod
    def strip_summary(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_actionable_steps(self) -> PlannerPayload:
        if not self.implementation_steps:
            raise ValueError("planner contract must include at least one implementation step")
        return self


class ReviewFindingPayload(StrictPayload):
    severity: Literal["info", "low", "medium", "high", "critical"]
    file: str
    line: int | None = Field(default=None, ge=1)
    message: str = Field(min_length=1)
    blocking: bool

    @model_validator(mode="before")
    @classmethod
    def normalize_finding(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        finding: dict[str, Any] = dict(data)
        blocking = finding.get("blocking") if isinstance(finding.get("blocking"), bool) else None
        finding["severity"] = _normalize_review_severity(finding.get("severity"), blocking)
        return finding

    @field_validator("severity", mode="before")
    @classmethod
    def normalize_severity(cls, value: object) -> object:
        return _normalize_review_severity(value, None)


class ReviewPayload(StrictPayload):
    findings: list[ReviewFindingPayload] = Field(default_factory=list)


def _normalize_review_severity(value: object, blocking: bool | None) -> object:
    if not isinstance(value, str):
        return value
    normalized = re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).strip()
    if not normalized:
        return value
    if normalized in {"info", "low", "medium", "high", "critical"}:
        return normalized

    aliases = {
        "informational": "info",
        "note": "info",
        "notice": "info",
        "none": "info",
        "ok": "info",
        "nit": "low",
        "nitpick": "low",
        "suggestion": "low",
        "recommendation": "low",
        "optional": "low",
        "minor": "low",
        "small": "low",
        "non blocking": "low",
        "nonblocking": "low",
        "warning": "medium",
        "warn": "medium",
        "concern": "medium",
        "caution": "medium",
        "moderate": "medium",
        "normal": "medium",
        "major": "high",
        "serious": "high",
        "error": "high",
        "bug": "high",
        "failure": "high",
        "failing": "high",
        "regression": "high",
        "blocker": "critical",
        "blocking": "critical",
        "fatal": "critical",
        "severe": "critical",
        "catastrophic": "critical",
        "security": "critical",
        "vulnerability": "critical",
        "data loss": "critical",
    }
    if normalized in aliases:
        return aliases[normalized]

    for marker in ("critical", "blocker", "fatal", "security", "vulnerability"):
        if marker in normalized:
            return "critical"
    for marker in ("major", "serious", "error", "bug", "failure", "regression"):
        if marker in normalized:
            return "high"
    for marker in ("warning", "concern", "caution", "moderate"):
        if marker in normalized:
            return "medium"
    for marker in ("minor", "nit", "suggestion", "optional", "non blocking"):
        if marker in normalized:
            return "low"
    for marker in ("info", "note", "notice"):
        if marker in normalized:
            return "info"

    if blocking is True:
        return "high"
    if blocking is False:
        return "low"
    return "medium"
