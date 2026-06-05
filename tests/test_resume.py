from __future__ import annotations

import unittest

from autobot.models import Issue, IssueComment, IssueRecord, IssueState
from autobot.resume import resume_if_answered


class ResumeTests(unittest.TestCase):
    def test_resume_redacts_secret_like_human_reply_body(self) -> None:
        token = "ghp_" + ("A" * 36)
        record = IssueRecord("owner/repo", 1)
        record.transition(IssueState.WAITING)
        record.conversation["resume_after_comment_id"] = 7
        issue = Issue(
            "owner/repo",
            1,
            "Clarified change",
            "Body",
            "alice",
            [],
            [IssueComment(8, "alice", f"Use this token: {token}", "2026-06-05T00:01:00Z")],
        )

        resumed = resume_if_answered(record, issue, "bot")

        self.assertTrue(resumed)
        reply = record.conversation["human_replies"][0]
        self.assertNotIn(token, reply["body"])
        self.assertIn("[redacted-secret]", reply["body"])


if __name__ == "__main__":
    unittest.main()
