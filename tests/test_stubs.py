from __future__ import annotations

import unittest

from autobot.models import Issue
from autobot.stubs import JiraIssueTracker, LinearIssueTracker, SlackChatChannel


class StubTests(unittest.TestCase):
    def test_issue_tracker_stubs_raise_clearly(self) -> None:
        for tracker in (LinearIssueTracker(), JiraIssueTracker()):
            with (
                self.subTest(tracker=tracker.__class__.__name__),
                self.assertRaisesRegex(NotImplementedError, "documented stub"),
            ):
                tracker.list_actionable("PROJ")

    def test_slack_stub_raises_clearly(self) -> None:
        issue = Issue("owner/repo", 1, "Title", "Body", "alice", [])

        with self.assertRaisesRegex(NotImplementedError, "documented stub"):
            SlackChatChannel().notify(issue, "hello")


if __name__ == "__main__":
    unittest.main()
