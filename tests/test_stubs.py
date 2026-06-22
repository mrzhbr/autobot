from __future__ import annotations

import unittest

from autobot.linear import LinearError
from autobot.models import Issue
from autobot.stubs import JiraIssueTracker, LinearIssueTracker, SlackChatChannel


class StubTests(unittest.TestCase):
    def test_issue_tracker_stubs_raise_clearly(self) -> None:
        with self.assertRaisesRegex(NotImplementedError, "documented stub"):
            JiraIssueTracker().list_actionable("PROJ")

    def test_linear_tracker_is_implemented(self) -> None:
        with self.assertRaisesRegex(LinearError, "LINEAR_API_KEY is required"):
            LinearIssueTracker(None, team_key="ENG").list_actionable("owner/repo")

    def test_slack_stub_raises_clearly(self) -> None:
        issue = Issue("owner/repo", 1, "Title", "Body", "alice", [])

        with self.assertRaisesRegex(NotImplementedError, "documented stub"):
            SlackChatChannel().notify(issue, "hello")


if __name__ == "__main__":
    unittest.main()
