from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from autobot.github import GitHubError, GitHubGitHost, GitHubIssueTracker


class RecordingGitHost(GitHubGitHost):
    def __init__(self) -> None:
        super().__init__("token")
        self.commands: list[list[str]] = []

    def _run(self, cmd: list[str], cwd: Path | None = None) -> str:
        self.commands.append(cmd)
        return ""

    def _default_branch(self, repo: str) -> str:
        return "main"


class RecordingTracker:
    requests: list[tuple[str, str, dict | None]] = []

    def __init__(self, token: str | None, agent_login: str | None) -> None:
        self.token = token
        self.agent_login = agent_login

    def _request(self, method: str, path: str, body: dict | None = None):
        self.requests.append((method, path, body))
        return {"html_url": "https://github.test/pull/1"}


class PagedIssueTracker(GitHubIssueTracker):
    def __init__(self) -> None:
        super().__init__("token", "bot")
        self.requests: list[tuple[str, str]] = []

    def _request(self, method: str, path: str, body: dict | None = None):
        self.requests.append((method, path))
        if path == "/repos/owner/repo/issues/7":
            return {
                "number": 7,
                "title": "Clarified feature",
                "body": "Please implement this.",
                "user": {"login": "alice"},
                "labels": [{"name": "agent-ready"}],
            }
        if path == "/repos/owner/repo/issues/7/comments?per_page=100&page=1":
            return [
                {
                    "id": index,
                    "body": f"comment {index}",
                    "created_at": "2026-06-05T00:00:00Z",
                    "user": {"login": "alice"},
                }
                for index in range(1, 101)
            ]
        if path == "/repos/owner/repo/issues/7/comments?per_page=100&page=2":
            return [
                {
                    "id": 101,
                    "body": "Use the compact option.",
                    "created_at": "2026-06-05T00:01:00Z",
                    "user": {"login": "alice"},
                }
            ]
        raise AssertionError(path)


class LinkedIssueTracker(PagedIssueTracker):
    def _request(self, method: str, path: str, body: dict | None = None):
        if path.endswith("comments?per_page=100&page=1"):
            self._last_response_headers = {
                "Link": (
                    "<https://api.github.com/repos/owner/repo/issues/7/comments"
                    '?per_page=100&page=9>; rel="last"'
                )
            }
            self.requests.append((method, path))
            return [
                {
                    "id": index,
                    "body": f"comment {index}",
                    "created_at": "2026-06-05T00:00:00Z",
                    "user": {"login": "alice"},
                }
                for index in range(1, 101)
            ]
        if path.endswith("comments?per_page=100&page=9"):
            self.requests.append((method, path))
            return [
                {
                    "id": 901,
                    "body": "Latest human reply.",
                    "created_at": "2026-06-05T00:09:00Z",
                    "user": {"login": "alice"},
                }
            ]
        return super()._request(method, path, body)


class GitHubSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        RecordingTracker.requests = []

    def test_refuses_default_like_branch_names(self) -> None:
        host = RecordingGitHost()

        with self.assertRaisesRegex(GitHubError, "protected default-like branch"):
            host.create_branch(Path("/tmp/repo"), "main")
        with self.assertRaisesRegex(GitHubError, "protected default-like branch"):
            host.push("owner/repo", Path("/tmp/repo"), "master")

        self.assertEqual(host.commands, [])

    def test_push_uses_plain_branch_push_without_force(self) -> None:
        host = RecordingGitHost()

        host.push("owner/repo", Path("/tmp/repo"), "autobot/issue-1")

        command = host.commands[-1]
        self.assertIn("push", command)
        self.assertNotIn("--force", command)
        self.assertNotIn("--force-with-lease", command)
        self.assertEqual(command[-3:], ["push", "origin", "autobot/issue-1"])

    def test_open_pull_request_payload_is_always_draft(self) -> None:
        host = RecordingGitHost()

        with patch("autobot.github.GitHubIssueTracker", RecordingTracker):
            url = host.open_draft_pr("owner/repo", "autobot/issue-1", "Draft: title", "body")

        self.assertEqual(url, "https://github.test/pull/1")
        method, path, body = RecordingTracker.requests[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/repos/owner/repo/pulls")
        assert body is not None
        self.assertEqual(body["head"], "autobot/issue-1")
        self.assertEqual(body["base"], "main")
        self.assertEqual(body["draft"], True)

    def test_issue_get_paginates_comments(self) -> None:
        tracker = PagedIssueTracker()

        issue = tracker.get("owner/repo", 7)

        self.assertEqual(len(issue.comments), 101)
        self.assertEqual(issue.comments[-1].body, "Use the compact option.")
        self.assertIn(
            ("GET", "/repos/owner/repo/issues/7/comments?per_page=100&page=2"),
            tracker.requests,
        )

    def test_issue_get_uses_linked_last_comment_page(self) -> None:
        tracker = LinkedIssueTracker()

        issue = tracker.get("owner/repo", 7)

        self.assertEqual(issue.comments[-1].body, "Latest human reply.")
        self.assertIn(
            ("GET", "/repos/owner/repo/issues/7/comments?per_page=100&page=9"),
            tracker.requests,
        )
        self.assertNotIn(
            ("GET", "/repos/owner/repo/issues/7/comments?per_page=100&page=2"),
            tracker.requests,
        )


if __name__ == "__main__":
    unittest.main()
