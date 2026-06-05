from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from autobot import cli
from autobot.models import IssueState, ProcessResult


class FakeWatchTracker:
    def __init__(self, numbers: list[int]) -> None:
        self.numbers = numbers

    def list_actionable(self, repo: str) -> list[int]:
        return self.numbers


class FakeWatchProcessor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def process(self, repo: str, issue_number: int) -> ProcessResult:
        self.calls.append((repo, issue_number))
        return ProcessResult(
            state=IssueState.PR_OPEN,
            message="opened draft pull request",
            pr_url="dry-run://draft-pr",
            cost={"wall_seconds": 0.1},
            branch=f"autobot/issue-{issue_number}",
            review_rounds=1,
            files_touched=["README.md"],
            blocked_on=None,
        )


class CliTests(unittest.TestCase):
    def test_top_level_errors_redact_token_like_values(self) -> None:
        token = "ghp_" + ("A" * 36)

        with (
            patch("autobot.cli._run", side_effect=RuntimeError(f"failed with {token}")),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1", "--dry-run"])

        self.assertEqual(code, 1)
        self.assertNotIn(token, stderr.getvalue())
        self.assertIn("[redacted-secret]", stderr.getvalue())

    def test_watch_once_processes_actionable_issues_sequentially(self) -> None:
        tracker = FakeWatchTracker([2, 3])
        processor = FakeWatchProcessor()

        with (
            patch("autobot.cli.GitHubIssueTracker", return_value=tracker),
            patch("autobot.cli._processor", return_value=processor),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = cli.main(["watch", "--repo", "owner/repo", "--once", "--dry-run"])

        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(processor.calls, [("owner/repo", 2), ("owner/repo", 3)])
        self.assertEqual([line["issue"] for line in lines], [2, 3])
        self.assertTrue(all(line["state"] == "pr_open" for line in lines))

    def test_watch_once_reports_idle_when_no_actionable_issues(self) -> None:
        tracker = FakeWatchTracker([])
        processor = FakeWatchProcessor()

        with (
            patch("autobot.cli.GitHubIssueTracker", return_value=tracker),
            patch("autobot.cli._processor", return_value=processor),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = cli.main(["watch", "--repo", "owner/repo", "--once", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertEqual(processor.calls, [])
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"repo": "owner/repo", "state": "idle", "actionable": 0},
        )


if __name__ == "__main__":
    unittest.main()
