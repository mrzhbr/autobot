from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from autobot.scanner import find_secret_like_values


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class TriagePayload(StrictPayload):
    ready: bool
    questions: list[str] = Field(default_factory=list, max_length=3)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_question_when_blocked(self) -> TriagePayload:
        if not self.ready and not self.questions:
            raise ValueError("triage must include at least one question when not ready")
        return self

    @field_validator("questions")
    @classmethod
    def strip_questions(cls, questions: list[str]) -> list[str]:
        return [question.strip() for question in questions if question.strip()]


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


class ReviewFindingPayload(StrictPayload):
    severity: Literal["info", "low", "medium", "high", "critical"]
    file: str
    line: int | None = Field(default=None, ge=1)
    message: str = Field(min_length=1)
    blocking: bool


class ReviewPayload(StrictPayload):
    findings: list[ReviewFindingPayload] = Field(default_factory=list)
