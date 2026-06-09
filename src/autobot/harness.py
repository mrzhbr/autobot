from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from autobot.adapters import LLM
from autobot.config import Config
from autobot.models import ContextFile, FileChange, ImplementationPlan, Issue, Usage
from autobot.sandbox import DockerSandbox


class HarnessError(RuntimeError):
    pass


class HarnessTaskKind(StrEnum):
    PLANNING = "planning"
    TEST_AUTHOR = "test_author"
    IMPLEMENT = "implement"
    VERIFICATION_FIX = "verification_fix"
    REVIEW_FIX = "review_fix"


@dataclass(frozen=True)
class HarnessTask:
    kind: HarnessTaskKind
    issue: Issue
    context: list[ContextFile]
    review_findings: list[str] = field(default_factory=list)
    planning_context: str | None = None


@dataclass(frozen=True)
class HarnessResult:
    plan: list[str]
    changes: list[FileChange]
    test_commands: list[str]
    usage: Usage | None = None
    applied_in_workspace: bool = False
    changed_paths: list[str] = field(default_factory=list)
    transcript_path: str | None = None

    @classmethod
    def from_plan(
        cls,
        plan: ImplementationPlan,
        *,
        applied_in_workspace: bool = False,
        changed_paths: list[str] | None = None,
        transcript_path: str | None = None,
    ) -> HarnessResult:
        paths = changed_paths or [change.path for change in plan.changes]
        return cls(
            plan=plan.plan,
            changes=plan.changes,
            test_commands=plan.test_commands,
            usage=plan.usage,
            applied_in_workspace=applied_in_workspace,
            changed_paths=list(dict.fromkeys(paths)),
            transcript_path=transcript_path,
        )


@dataclass(frozen=True)
class PlanningResult:
    summary: str
    target_files: list[str]
    constraints: list[str]
    implementation_steps: list[str]
    tests_to_add: list[str]
    verification_commands: list[str]
    risks: list[str]
    non_goals: list[str]
    usage: Usage | None = None
    transcript_path: str | None = None

    def model_dump(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "target_files": self.target_files,
            "constraints": self.constraints,
            "implementation_steps": self.implementation_steps,
            "tests_to_add": self.tests_to_add,
            "verification_commands": self.verification_commands,
            "risks": self.risks,
            "non_goals": self.non_goals,
            "transcript_path": self.transcript_path,
        }

    def as_prompt_context(self) -> str:
        return "\n".join(
            [
                f"Summary: {self.summary}",
                _section("Target files", self.target_files),
                _section("Constraints", self.constraints),
                _section("Implementation steps", self.implementation_steps),
                _section("Tests to add", self.tests_to_add),
                _section("Verification commands", self.verification_commands),
                _section("Risks", self.risks),
                _section("Non-goals", self.non_goals),
            ]
        )


class HarnessSession(Protocol):
    def plan(self, task: HarnessTask) -> PlanningResult:
        """Run one read-only planning task in the repository."""

    def run(self, task: HarnessTask) -> HarnessResult:
        """Run one implementation task in a persistent harness session."""

    def close(self) -> None:
        """Release harness resources."""


class HarnessAdapter(Protocol):
    def start(
        self,
        repo_dir: Path,
        sandbox: DockerSandbox | None = None,
        dry_run: bool = False,
    ) -> HarnessSession:
        """Start a per-issue harness session for repo_dir."""


class LegacyLLMHarnessAdapter:
    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def start(
        self,
        repo_dir: Path,
        sandbox: DockerSandbox | None = None,
        dry_run: bool = False,
    ) -> HarnessSession:
        return LegacyLLMHarnessSession(self.llm)


class LegacyLLMHarnessSession:
    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def plan(self, task: HarnessTask) -> PlanningResult:
        return PlanningResult(
            summary="Planner disabled for legacy dry-run harness.",
            target_files=[item.path for item in task.context],
            constraints=["Keep changes scoped to the issue."],
            implementation_steps=["Use the implementation harness to inspect and edit files."],
            tests_to_add=["Add tests derived from the issue acceptance criteria."],
            verification_commands=[],
            risks=[],
            non_goals=[],
            usage=None,
        )

    def run(self, task: HarnessTask) -> HarnessResult:
        if task.kind == HarnessTaskKind.TEST_AUTHOR:
            return HarnessResult.from_plan(self.llm.write_tests(task.issue, task.context))
        if task.kind in {
            HarnessTaskKind.IMPLEMENT,
            HarnessTaskKind.VERIFICATION_FIX,
            HarnessTaskKind.REVIEW_FIX,
        }:
            findings = task.review_findings or None
            return HarnessResult.from_plan(self.llm.implement(task.issue, task.context, findings))
        raise HarnessError(f"unknown harness task kind: {task.kind}")

    def close(self) -> None:
        return None


class UnavailableHarnessAdapter:
    def __init__(self, name: str) -> None:
        self.name = name

    def start(
        self,
        repo_dir: Path,
        sandbox: DockerSandbox | None = None,
        dry_run: bool = False,
    ) -> HarnessSession:
        raise HarnessError(
            f"IMPLEMENT_HARNESS={self.name} is configured, but the {self.name} "
            "adapter is not wired yet"
        )


def build_harness_adapter(config: Config, llm: LLM) -> HarnessAdapter:
    if config.dry_run or config.mock_llm:
        return LegacyLLMHarnessAdapter(llm)
    if config.implement_harness == "legacy":
        return LegacyLLMHarnessAdapter(llm)
    if config.implement_harness == "pi":
        from autobot.pi_harness import PiHarnessAdapter

        return PiHarnessAdapter(config)
    if config.implement_harness == "openhands":
        return UnavailableHarnessAdapter(config.implement_harness)
    raise HarnessError(f"unknown implementation harness: {config.implement_harness}")


def _section(title: str, items: list[str]) -> str:
    if not items:
        return f"{title}:\n- None"
    return title + ":\n" + "\n".join(f"- {item}" for item in items)
