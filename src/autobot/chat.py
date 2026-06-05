from __future__ import annotations

from autobot.adapters import IssueTracker
from autobot.models import Issue
from autobot.scanner import redact_secret_like_values


class IssueCommentChat:
    def __init__(self, tracker: IssueTracker) -> None:
        self.tracker = tracker

    def ask(self, issue: Issue, questions: list[str]) -> int:
        intro = f"@{issue.author} I need one clarification before I can implement this:"
        body = "\n".join(f"{index}. {question}" for index, question in enumerate(questions, 1))
        return self.tracker.comment(
            issue.repo,
            issue.number,
            redact_secret_like_values(f"{intro}\n\n{body}"),
        )

    def notify(self, issue: Issue, text: str) -> int:
        return self.tracker.comment(issue.repo, issue.number, redact_secret_like_values(text))
