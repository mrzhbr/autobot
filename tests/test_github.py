from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from autobot.github import GitHubError, GitHubGitHost


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


if __name__ == "__main__":
    unittest.main()
