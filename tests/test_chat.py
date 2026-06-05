from __future__ import annotations

import unittest

from autobot.chat import IssueCommentChat
from autobot.models import Issue


class FakeTracker:
    def __init__(self) -> None:
        self.comments: list[str] = []

    def comment(self, repo: str, issue_number: int, text: str) -> int:
        self.comments.append(text)
        return len(self.comments)


class ChatTests(unittest.TestCase):
    def test_comments_redact_token_like_values(self) -> None:
        token = "ghp_" + ("A" * 36)
        issue = Issue("owner/repo", 1, "Title", "Body", "alice", [])
        tracker = FakeTracker()
        chat = IssueCommentChat(tracker)

        chat.ask(issue, [f"Can you confirm {token}?"])
        chat.notify(issue, f"Paused with {token}")

        self.assertEqual(len(tracker.comments), 2)
        for comment in tracker.comments:
            self.assertNotIn(token, comment)
            self.assertIn("[redacted-secret]", comment)


if __name__ == "__main__":
    unittest.main()
