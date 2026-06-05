from __future__ import annotations

from pathlib import Path
from typing import Protocol

from autobot.models import (
    ContextFile,
    ImplementationPlan,
    Issue,
    ReviewReport,
    TriageDecision,
)


class IssueTracker(Protocol):
    def list_actionable(self, repo: str) -> list[int]:
        """Return open issue numbers assigned to or mentioning this agent."""

    def get(self, repo: str, issue_number: int) -> Issue:
        """Return the issue body, labels, author, and comments."""

    def comment(self, repo: str, issue_number: int, text: str) -> int:
        """Post a comment and return the remote comment id."""

    def set_label(self, repo: str, issue_number: int, label: str) -> None:
        """Apply a state label."""


class GitHost(Protocol):
    def clone(self, repo: str, target_dir: Path) -> None:
        """Clone the repository into target_dir."""

    def create_branch(self, repo_dir: Path, branch: str) -> None:
        """Create and check out a working branch."""

    def current_diff(self, repo_dir: Path) -> str:
        """Return the working tree diff."""

    def commit_all(self, repo_dir: Path, message: str) -> bool:
        """Commit all changes. Return False when there is nothing to commit."""

    def push(self, repo: str, repo_dir: Path, branch: str) -> None:
        """Push branch without force."""

    def open_draft_pr(self, repo: str, branch: str, title: str, body: str) -> str:
        """Open a draft pull request and return its URL."""

    def ci_status(self, repo: str, branch: str) -> dict:
        """Return CI status information for branch."""


class ChatChannel(Protocol):
    def ask(self, issue: Issue, questions: list[str]) -> int:
        """Post one batched clarification comment and return the comment id."""

    def notify(self, issue: Issue, text: str) -> int:
        """Post a status notification and return the comment id."""


class LLM(Protocol):
    def triage(self, issue: Issue, context: list[ContextFile]) -> TriageDecision:
        """Decide whether an issue is specified enough to implement."""

    def implement(
        self,
        issue: Issue,
        context: list[ContextFile],
        review_findings: list[str] | None = None,
    ) -> ImplementationPlan:
        """Return a concrete plan, full-file changes, and test commands."""

    def write_tests(self, issue: Issue, context: list[ContextFile]) -> ImplementationPlan:
        """Return acceptance-test changes derived from the issue spec."""

    def review(
        self,
        lens: str,
        issue: Issue,
        diff: str,
        model: str | None = None,
    ) -> ReviewReport:
        """Review a diff through one review lens."""
