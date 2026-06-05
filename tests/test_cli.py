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
    def __init__(self, failures: dict[int, Exception] | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.failures = failures or {}

    def process(self, repo: str, issue_number: int) -> ProcessResult:
        self.calls.append((repo, issue_number))
        if issue_number in self.failures:
            raise self.failures[issue_number]
        return ProcessResult(
            state=IssueState.PR_OPEN,
            message="opened draft pull request",
            pr_url="dry-run://draft-pr",
            cost={"wall_seconds": 0.1},
            branch=f"autobot/issue-{issue_number}",
            review_rounds=1,
            files_touched=["README.md"],
            verification_commands=["python -m pytest"],
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

    def test_live_run_fails_before_processor_without_llm_key(self) -> None:
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "x"}, clear=True),
            patch("autobot.cli._processor") as processor,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1"])

        self.assertEqual(code, 1)
        processor.assert_not_called()
        self.assertIn("OPENAI_API_KEY or ANTHROPIC_API_KEY is required", stderr.getvalue())

    def test_live_run_requires_provider_specific_llm_key(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {"GITHUB_TOKEN": "x", "LLM_PROVIDER": "openai"},
                clear=True,
            ),
            patch("autobot.cli._processor") as processor,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1"])

        self.assertEqual(code, 1)
        processor.assert_not_called()
        self.assertIn("OPENAI_API_KEY is required", stderr.getvalue())

    def test_live_run_rejects_unknown_llm_provider_before_processor(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {"GITHUB_TOKEN": "x", "LLM_PROVIDER": "bogus", "OPENAI_API_KEY": "x"},
                clear=True,
            ),
            patch("autobot.cli._processor") as processor,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1"])

        self.assertEqual(code, 1)
        processor.assert_not_called()
        self.assertIn("LLM_PROVIDER must be openai or anthropic", stderr.getvalue())

    def test_live_run_rejects_review_model_when_matching_key_is_missing(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "GITHUB_TOKEN": "x",
                    "OPENAI_API_KEY": "x",
                    "REVIEW_MODELS": "gpt-4.1,claude-sonnet-4-20250514",
                },
                clear=True,
            ),
            patch("autobot.cli._processor") as processor,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1"])

        self.assertEqual(code, 1)
        processor.assert_not_called()
        self.assertIn("ANTHROPIC_API_KEY", stderr.getvalue())
        self.assertIn("claude-sonnet-4-20250514", stderr.getvalue())

    def test_live_run_accepts_mixed_review_models_when_keys_exist(self) -> None:
        processor = FakeWatchProcessor()
        with (
            patch.dict(
                "os.environ",
                {
                    "GITHUB_TOKEN": "x",
                    "OPENAI_API_KEY": "x",
                    "ANTHROPIC_API_KEY": "x",
                    "REVIEW_MODELS": "gpt-4.1,claude-sonnet-4-20250514",
                },
                clear=True,
            ),
            patch("autobot.cli._processor", return_value=processor) as build_processor,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1"])

        self.assertEqual(code, 0)
        self.assertIn('"state": "pr_open"', stdout.getvalue())
        config = build_processor.call_args.args[0]
        self.assertEqual(config.review_models, ["gpt-4.1", "claude-sonnet-4-20250514"])

    def test_live_run_accepts_anthropic_key_without_openai_key(self) -> None:
        processor = FakeWatchProcessor()
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "x", "ANTHROPIC_API_KEY": "x"}, clear=True),
            patch("autobot.cli._processor", return_value=processor) as build_processor,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1"])

        self.assertEqual(code, 0)
        self.assertIn('"state": "pr_open"', stdout.getvalue())
        config = build_processor.call_args.args[0]
        self.assertEqual(config.implement_model, "claude-sonnet-4-20250514")

    def test_live_watch_fails_before_tracker_without_llm_key(self) -> None:
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "x"}, clear=True),
            patch("autobot.cli.GitHubIssueTracker") as tracker,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["watch", "--repo", "owner/repo", "--once"])

        self.assertEqual(code, 1)
        tracker.assert_not_called()
        self.assertIn("OPENAI_API_KEY or ANTHROPIC_API_KEY is required", stderr.getvalue())

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
        self.assertTrue(
            all(line["verification_commands"] == ["python -m pytest"] for line in lines)
        )

    def test_watch_once_continues_after_issue_failure(self) -> None:
        token = "ghp_" + ("A" * 36)
        tracker = FakeWatchTracker([2, 3])
        processor = FakeWatchProcessor({2: RuntimeError(f"failed with {token}")})

        with (
            patch("autobot.cli.GitHubIssueTracker", return_value=tracker),
            patch("autobot.cli._processor", return_value=processor),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = cli.main(["watch", "--repo", "owner/repo", "--once", "--dry-run"])

        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(code, 1)
        self.assertEqual(processor.calls, [("owner/repo", 2), ("owner/repo", 3)])
        self.assertEqual(lines[0]["state"], "error")
        self.assertEqual(lines[0]["issue"], 2)
        self.assertNotIn(token, lines[0]["message"])
        self.assertIn("[redacted-secret]", lines[0]["message"])
        self.assertEqual(lines[1]["state"], "pr_open")
        self.assertEqual(lines[1]["issue"], 3)

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
