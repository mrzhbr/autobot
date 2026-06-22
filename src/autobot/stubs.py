from __future__ import annotations

from autobot.linear import LinearIssueTracker as LinearIssueTracker
from autobot.models import Issue


class JiraIssueTracker:
    """Documented stub for a future Jira IssueTracker adapter.

    Expected mapping:
    - `repo` becomes a Jira project or configured board key.
    - `issue_number` becomes a local issue id mapped to a Jira key.
    - Comments and labels map to Jira comments and labels.
    """

    def list_actionable(self, repo: str) -> list[int]:
        raise NotImplementedError("Jira adapter is a documented stub for this prototype")

    def get(self, repo: str, issue_number: int) -> Issue:
        raise NotImplementedError("Jira adapter is a documented stub for this prototype")

    def comment(self, repo: str, issue_number: int, text: str) -> int:
        raise NotImplementedError("Jira adapter is a documented stub for this prototype")

    def set_label(self, repo: str, issue_number: int, label: str) -> None:
        raise NotImplementedError("Jira adapter is a documented stub for this prototype")


class SlackChatChannel:
    """Documented stub for a future Slack ChatChannel adapter.

    `ask` should post one message and return immediately. Human answers should be
    folded back into the issue context by the tracker or a configured bridge.
    """

    def ask(self, issue: Issue, questions: list[str]) -> int:
        raise NotImplementedError("Slack chat channel is a documented stub for this prototype")

    def notify(self, issue: Issue, text: str) -> int:
        raise NotImplementedError("Slack chat channel is a documented stub for this prototype")
