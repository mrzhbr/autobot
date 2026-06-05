from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autobot.github import GitHubError, GitHubGitHost, GitHubIssueTracker


class FakeHTTPResponse:
    headers: dict = {}

    def read(self) -> bytes:
        return b'{"ok": true}'

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


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


class CITracker:
    requests: list[tuple[str, str, dict | None]] = []
    responses: dict[str, dict] = {}
    errors: dict[str, GitHubError] = {}

    def __init__(self, token: str | None, agent_login: str | None) -> None:
        self.token = token
        self.agent_login = agent_login

    def _request(self, method: str, path: str, body: dict | None = None):
        self.requests.append((method, path, body))
        if path in self.errors:
            raise self.errors[path]
        return self.responses[path]


class CommentRecordingTracker(GitHubIssueTracker):
    def __init__(self) -> None:
        super().__init__("token", "bot")
        self.requests: list[tuple[str, str, dict | None]] = []

    def _request(self, method: str, path: str, body: dict | None = None):
        self.requests.append((method, path, body))
        return {"id": 123}


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


class ActionableLabelTracker(GitHubIssueTracker):
    def __init__(self) -> None:
        super().__init__("token", None)
        self.requests: list[tuple[str, str]] = []

    def _request(self, method: str, path: str, body: dict | None = None):
        self.requests.append((method, path))
        page = urllib.parse.parse_qs(urllib.parse.urlparse(path).query).get("page", [""])[0]
        self._last_response_headers = {}
        if page == "1":
            self._last_response_headers = {
                "Link": (
                    "<https://api.github.com/repos/owner/repo/issues"
                    '?state=open&labels=agent-ready&per_page=100&page=2>; rel="last"'
                )
            }
            return [
                {"id": 1, "number": 7},
                {"id": 2, "number": 8, "pull_request": {}},
            ]
        if page == "2":
            return [{"id": 3, "number": 9}]
        raise AssertionError(path)


class ActionableSearchTracker(GitHubIssueTracker):
    def __init__(self) -> None:
        super().__init__("token", "bot")
        self.requests: list[tuple[str, str]] = []

    def _request(self, method: str, path: str, body: dict | None = None):
        self.requests.append((method, path))
        parsed = urllib.parse.urlparse(path)
        query = urllib.parse.parse_qs(parsed.query)
        page = query.get("page", [""])[0]
        search = query.get("q", [""])[0]
        self._last_response_headers = {}
        if "mentions:bot" in search and page == "1":
            self._last_response_headers = {
                "Link": (
                    "<https://api.github.com/search/issues"
                    '?q=mentions%3Abot&per_page=100&page=2>; rel="last"'
                )
            }
            return {
                "items": [
                    {"id": 1, "number": 7},
                    {"id": 2, "number": 8, "pull_request": {}},
                ]
            }
        if "mentions:bot" in search and page == "2":
            return {"items": [{"id": 3, "number": 9}]}
        if "assignee:bot" in search and page == "1":
            return {"items": [{"id": 3, "number": 9}, {"id": 4, "number": 11}]}
        raise AssertionError(path)


class LabelTracker(GitHubIssueTracker):
    def __init__(self, status_code: int = 422, create_status_code: int | None = None) -> None:
        super().__init__("token", "bot")
        self.status_code = status_code
        self.create_status_code = create_status_code
        self.requests: list[tuple[str, str, dict | None]] = []
        self.label_attempts = 0

    def _request(self, method: str, path: str, body: dict | None = None):
        self.requests.append((method, path, body))
        if path == "/repos/owner/repo/issues/7/labels":
            self.label_attempts += 1
            if self.label_attempts == 1:
                raise GitHubError("label failed", status_code=self.status_code)
            return {}
        if path == "/repos/owner/repo/labels":
            if self.create_status_code is not None:
                raise GitHubError("create label failed", status_code=self.create_status_code)
            return {"name": body["name"] if body else ""}
        return {}


class GitHubSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        RecordingTracker.requests = []
        CITracker.requests = []
        CITracker.responses = {}
        CITracker.errors = {}

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

    def test_git_command_errors_redact_token_like_values(self) -> None:
        token = "ghp_" + ("A" * 36)
        failed = SimpleNamespace(returncode=1, stdout="", stderr=f"fatal: {token}\n")
        host = GitHubGitHost(token)

        with (
            patch("autobot.github.subprocess.run", return_value=failed),
            self.assertRaises(GitHubError) as raised,
        ):
            host.push("owner/repo", Path("/tmp/repo"), "autobot/issue-1")

        self.assertNotIn(token, str(raised.exception))
        self.assertIn("[redacted-secret]", str(raised.exception))

    def test_http_errors_redact_token_like_payloads(self) -> None:
        token = "ghp_" + ("A" * 36)
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo",
            403,
            "Forbidden",
            {},
            io.BytesIO(f'{{"message":"bad {token}"}}'.encode()),
        )
        tracker = GitHubIssueTracker(token, "bot")

        with (
            patch("autobot.github.urllib.request.urlopen", side_effect=error),
            self.assertRaises(GitHubError) as raised,
        ):
            tracker.get("owner/repo", 1)

        self.assertNotIn(token, str(raised.exception))
        self.assertIn("[redacted-secret]", str(raised.exception))

    def test_url_errors_are_wrapped_and_redacted(self) -> None:
        token = "ghp_" + ("A" * 36)
        error = urllib.error.URLError(f"network down {token}")
        tracker = GitHubIssueTracker(token, "bot")

        with (
            patch("autobot.github.urllib.request.urlopen", side_effect=error),
            self.assertRaises(GitHubError) as raised,
        ):
            tracker.get("owner/repo", 1)

        self.assertIsNone(raised.exception.status_code)
        self.assertNotIn(token, str(raised.exception))
        self.assertIn("[redacted-secret]", str(raised.exception))

    def test_comment_payload_redacts_token_like_values(self) -> None:
        token = "ghp_" + ("A" * 36)
        tracker = CommentRecordingTracker()

        comment_id = tracker.comment("owner/repo", 7, f"failed with {token}")

        self.assertEqual(comment_id, 123)
        body = tracker.requests[-1][2]
        assert body is not None
        self.assertEqual(body["body"], "failed with [redacted-secret]")

    def test_reused_clone_is_reset_to_remote_default_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            (repo_dir / ".git").mkdir(parents=True)
            host = RecordingGitHost()

            host.clone("owner/repo", repo_dir)

        self.assertEqual(
            host.commands,
            [
                [
                    "git",
                    "-c",
                    "http.https://github.com/.extraheader=AUTHORIZATION: bearer token",
                    "fetch",
                    "origin",
                    "--prune",
                ],
                ["git", "checkout", "-B", "main", "origin/main"],
                ["git", "reset", "--hard", "origin/main"],
                ["git", "clean", "-fd"],
            ],
        )

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

    def test_open_pull_request_payload_redacts_token_like_values(self) -> None:
        token = "ghp_" + ("A" * 36)
        host = RecordingGitHost()

        with patch("autobot.github.GitHubIssueTracker", RecordingTracker):
            host.open_draft_pr(
                "owner/repo",
                "autobot/issue-1",
                f"Draft: {token}",
                f"body {token}",
            )

        body = RecordingTracker.requests[-1][2]
        assert body is not None
        self.assertNotIn(token, body["title"])
        self.assertNotIn(token, body["body"])
        self.assertIn("[redacted-secret]", body["title"])
        self.assertIn("[redacted-secret]", body["body"])

    def test_ci_status_combines_commit_statuses_and_check_runs(self) -> None:
        CITracker.responses = {
            "/repos/owner/repo/commits/autobot%2Fissue-1/status": {
                "state": "success",
                "statuses": [{"context": "legacy", "state": "success"}],
            },
            "/repos/owner/repo/commits/autobot%2Fissue-1/check-runs": {
                "check_runs": [
                    {
                        "name": "tests",
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": "https://github.test/checks/1",
                        "output": {"summary": "large payload omitted from summary"},
                    }
                ]
            },
        }
        host = RecordingGitHost()

        with patch("autobot.github.GitHubIssueTracker", CITracker):
            status = host.ci_status("owner/repo", "autobot/issue-1")

        self.assertEqual(status["state"], "success")
        self.assertEqual(status["statuses"], [{"context": "legacy", "state": "success"}])
        self.assertEqual(
            status["check_runs"],
            [
                {
                    "name": "tests",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.test/checks/1",
                }
            ],
        )
        self.assertEqual(
            [request[:2] for request in CITracker.requests],
            [
                ("GET", "/repos/owner/repo/commits/autobot%2Fissue-1/status"),
                ("GET", "/repos/owner/repo/commits/autobot%2Fissue-1/check-runs"),
            ],
        )

    def test_ci_status_marks_failed_check_run_as_failure(self) -> None:
        CITracker.responses = {
            "/repos/owner/repo/commits/autobot%2Fissue-1/status": {
                "state": "success",
                "statuses": [{"context": "legacy", "state": "success"}],
            },
            "/repos/owner/repo/commits/autobot%2Fissue-1/check-runs": {
                "check_runs": [{"name": "tests", "status": "completed", "conclusion": "failure"}]
            },
        }
        host = RecordingGitHost()

        with patch("autobot.github.GitHubIssueTracker", CITracker):
            status = host.ci_status("owner/repo", "autobot/issue-1")

        self.assertEqual(status["state"], "failure")

    def test_ci_status_uses_successful_checks_when_legacy_statuses_are_empty(self) -> None:
        CITracker.responses = {
            "/repos/owner/repo/commits/autobot%2Fissue-1/status": {
                "state": "pending",
                "statuses": [],
            },
            "/repos/owner/repo/commits/autobot%2Fissue-1/check-runs": {
                "check_runs": [{"name": "tests", "status": "completed", "conclusion": "success"}]
            },
        }
        host = RecordingGitHost()

        with patch("autobot.github.GitHubIssueTracker", CITracker):
            status = host.ci_status("owner/repo", "autobot/issue-1")

        self.assertEqual(status["state"], "success")

    def test_ci_status_keeps_legacy_status_when_check_runs_request_fails(self) -> None:
        CITracker.responses = {
            "/repos/owner/repo/commits/autobot%2Fissue-1/status": {
                "state": "success",
                "statuses": [{"context": "legacy", "state": "success"}],
            }
        }
        CITracker.errors = {
            "/repos/owner/repo/commits/autobot%2Fissue-1/check-runs": GitHubError(
                "checks unavailable"
            )
        }
        host = RecordingGitHost()

        with patch("autobot.github.GitHubIssueTracker", CITracker):
            status = host.ci_status("owner/repo", "autobot/issue-1")

        self.assertEqual(status["state"], "success")
        self.assertEqual(status["errors"], ["checks unavailable"])

    def test_request_json_body_redacts_token_like_values(self) -> None:
        token = "ghp_" + ("A" * 36)
        tracker = GitHubIssueTracker("token", "bot")

        with patch(
            "autobot.github.urllib.request.urlopen", return_value=FakeHTTPResponse()
        ) as open_url:
            tracker._request("POST", "/repos/owner/repo/issues/7/labels", {"labels": [token]})

        request = open_url.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertNotIn(token, json.dumps(payload))
        self.assertEqual(payload["labels"], ["[redacted-secret]"])

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

    def test_list_actionable_paginates_agent_ready_label_fallback(self) -> None:
        tracker = ActionableLabelTracker()

        numbers = tracker.list_actionable("owner/repo")

        self.assertEqual(numbers, [7, 9])
        self.assertIn(
            ("GET", "/repos/owner/repo/issues?state=open&labels=agent-ready&per_page=100&page=2"),
            tracker.requests,
        )

    def test_list_actionable_paginates_mentions_and_assignments(self) -> None:
        tracker = ActionableSearchTracker()

        numbers = tracker.list_actionable("owner/repo")

        self.assertEqual(numbers, [7, 9, 11])
        self.assertTrue(
            any("mentions%3Abot" in path and "page=2" in path for _, path in tracker.requests)
        )
        self.assertTrue(any("assignee%3Abot" in path for _, path in tracker.requests))

    def test_set_label_creates_missing_repo_label_then_retries(self) -> None:
        tracker = LabelTracker()

        tracker.set_label("owner/repo", 7, "agent-waiting")

        self.assertEqual(
            tracker.requests,
            [
                ("POST", "/repos/owner/repo/issues/7/labels", {"labels": ["agent-waiting"]}),
                (
                    "POST",
                    "/repos/owner/repo/labels",
                    {"name": "agent-waiting", "color": "ededed"},
                ),
                ("POST", "/repos/owner/repo/issues/7/labels", {"labels": ["agent-waiting"]}),
            ],
        )

    def test_set_label_tolerates_label_create_race(self) -> None:
        tracker = LabelTracker(create_status_code=422)

        tracker.set_label("owner/repo", 7, "agent-waiting")

        self.assertEqual(tracker.label_attempts, 2)
        self.assertEqual(
            tracker.requests[-1],
            ("POST", "/repos/owner/repo/issues/7/labels", {"labels": ["agent-waiting"]}),
        )

    def test_set_label_does_not_mask_label_create_errors(self) -> None:
        tracker = LabelTracker(create_status_code=500)

        with self.assertRaisesRegex(GitHubError, "create label failed"):
            tracker.set_label("owner/repo", 7, "agent-waiting")

        self.assertEqual(tracker.label_attempts, 1)

    def test_set_label_does_not_mask_non_validation_errors(self) -> None:
        tracker = LabelTracker(status_code=500)

        with self.assertRaisesRegex(GitHubError, "label failed"):
            tracker.set_label("owner/repo", 7, "agent-waiting")

        self.assertEqual(len(tracker.requests), 1)


if __name__ == "__main__":
    unittest.main()
