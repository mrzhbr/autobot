from __future__ import annotations

import json
import unittest
import urllib.error
from unittest.mock import patch

from autobot.linear import LinearError, LinearIssueTracker
from autobot.models import IssueRecord, IssueState
from autobot.resume import resume_if_answered
from autobot.workflow_models import WorkflowConversation


class RecordingLinearTracker(LinearIssueTracker):
    def __init__(self, responses: list[dict]) -> None:
        super().__init__("lin_api_secret", "Autobot", "ENG")
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def _graphql(self, query: str, variables: dict) -> dict:
        self.calls.append((query, variables))
        return self.responses.pop(0)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class LinearTests(unittest.TestCase):
    def test_graphql_request_construction(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["auth"] = request.get_header("Authorization")
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse({"data": {"ok": True}})

        tracker = LinearIssueTracker("lin_api_secret", "Autobot", "ENG")
        with patch("urllib.request.urlopen", fake_urlopen):
            data = tracker._graphql("query Test { viewer { id } }", {"x": 1})

        self.assertEqual(data, {"ok": True})
        self.assertEqual(captured["url"], "https://api.linear.app/graphql")
        self.assertEqual(captured["timeout"], 30)
        self.assertEqual(captured["auth"], "lin_api_secret")
        self.assertEqual(captured["body"]["variables"], {"x": 1})

    def test_graphql_errors_redact_api_key(self) -> None:
        token = "lin_api_" + ("A" * 32)

        def fake_urlopen(request, timeout):
            raise urllib.error.URLError(f"failed with {token}")

        tracker = LinearIssueTracker(token, "Autobot", "ENG")
        with patch("urllib.request.urlopen", fake_urlopen), self.assertRaises(LinearError) as ctx:
            tracker._graphql("query Test { viewer { id } }", {})

        self.assertNotIn(token, str(ctx.exception))
        self.assertIn("[redacted-secret]", str(ctx.exception))

    def test_get_parses_issue_and_comments(self) -> None:
        tracker = RecordingLinearTracker(
            [
                {
                    "issue": {
                        "id": "issue-id",
                        "identifier": "ENG-123",
                        "title": "Add filter",
                        "description": "Use a dropdown.",
                        "creator": {"displayName": "Alice"},
                        "labels": {"nodes": [{"id": "label-1", "name": "agent-ready"}]},
                        "comments": {
                            "nodes": [
                                {
                                    "id": "comment-1",
                                    "body": "Please continue.",
                                    "createdAt": "2026-06-05T00:01:02.123Z",
                                    "user": {"name": "Bob"},
                                }
                            ]
                        },
                    }
                }
            ]
        )

        issue = tracker.get("owner/repo", 123)

        self.assertEqual(issue.repo, "owner/repo")
        self.assertEqual(issue.number, 123)
        self.assertEqual(issue.title, "Add filter")
        self.assertEqual(issue.body, "Use a dropdown.")
        self.assertEqual(issue.author, "Alice")
        self.assertEqual(issue.labels, ["agent-ready"])
        self.assertEqual(issue.comments[0].author, "Bob")
        self.assertEqual(issue.comments[0].body, "Please continue.")
        self.assertGreater(issue.comments[0].id, 0)
        self.assertEqual(tracker.calls[0][1], {"identifier": "ENG-123"})

    def test_list_actionable_uses_team_key_and_returns_numbers(self) -> None:
        tracker = RecordingLinearTracker(
            [{"issues": {"nodes": [{"identifier": "ENG-3"}, {"identifier": "ENG-2"}]}}]
        )

        numbers = tracker.list_actionable("owner/repo")

        self.assertEqual(numbers, [2, 3])
        query, variables = tracker.calls[0]
        self.assertIn("AutobotActionableForAgent", query)
        self.assertEqual(variables["teamKey"], "ENG")
        self.assertEqual(variables["agent"], "Autobot")

    def test_comment_creation_returns_timestamp_marker(self) -> None:
        tracker = RecordingLinearTracker(
            [
                {"issue": {"id": "issue-id"}},
                {
                    "commentCreate": {
                        "comment": {
                            "id": "comment-id",
                            "createdAt": "2026-06-05T00:01:02.123456Z",
                        }
                    }
                },
            ]
        )

        comment_id = tracker.comment("owner/repo", 123, "hello")

        self.assertEqual(comment_id, 1780617662123456)
        self.assertEqual(tracker.calls[0][1], {"identifier": "ENG-123"})
        self.assertEqual(tracker.calls[1][1], {"issueId": "issue-id", "body": "hello"})

    def test_set_label_creates_and_applies_missing_label(self) -> None:
        tracker = RecordingLinearTracker(
            [
                {
                    "issue": {
                        "id": "issue-id",
                        "team": {"id": "team-id"},
                        "labels": {"nodes": [{"id": "existing", "name": "bug"}]},
                    }
                },
                {"issueLabelCreate": {"issueLabel": {"id": "new-label", "name": "agent-waiting"}}},
                {"issueUpdate": {"success": True}},
            ]
        )

        tracker.set_label("owner/repo", 123, "agent-waiting")

        self.assertEqual(tracker.calls[1][1], {"teamId": "team-id", "name": "agent-waiting"})
        self.assertEqual(
            tracker.calls[2][1],
            {"id": "issue-id", "labelIds": ["existing", "new-label"]},
        )

    def test_linear_comments_resume_after_marker_and_ignore_bot(self) -> None:
        tracker = RecordingLinearTracker(
            [
                {
                    "issue": {
                        "identifier": "ENG-123",
                        "title": "Add filter",
                        "description": "Body",
                        "creator": {"name": "Alice"},
                        "labels": {"nodes": []},
                        "comments": {
                            "nodes": [
                                {
                                    "id": "bot",
                                    "body": "Autobot question",
                                    "createdAt": "2026-06-05T00:01:00Z",
                                    "user": {"name": "Autobot"},
                                },
                                {
                                    "id": "human",
                                    "body": "Use the compact option.",
                                    "createdAt": "2026-06-05T00:02:00Z",
                                    "user": {"name": "Alice"},
                                },
                            ]
                        },
                    }
                }
            ]
        )
        issue = tracker.get("owner/repo", 123)
        record = IssueRecord(
            repo="owner/repo",
            issue_number=123,
            state=IssueState.WAITING,
        )
        conversation = WorkflowConversation()
        conversation.record_clarification_pause(issue.comments[0].id, ["Which option?"])
        conversation.save(record)

        self.assertTrue(resume_if_answered(record, issue, "Autobot"))
        self.assertEqual(record.conversation["human_replies"][0]["body"], "Use the compact option.")


if __name__ == "__main__":
    unittest.main()
