from __future__ import annotations

from autobot.adapters import IssueTracker
from autobot.models import Issue


class IssueCommentChat:
    def __init__(self, tracker: IssueTracker) -> None:
        self.tracker = tracker

    def ask(self, issue: Issue, questions: list[str]) -> int:
        intro = f"@{issue.author} I need one clarification before I can implement this:"
        body = "\n".join(f"{index}. {question}" for index, question in enumerate(questions, 1))
        return self.tracker.comment(issue.repo, issue.number, f"{intro}\n\n{body}")

    def notify(self, issue: Issue, text: str) -> int:
        return self.tracker.comment(issue.repo, issue.number, text)
