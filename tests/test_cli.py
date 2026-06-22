from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autobot import cli
from autobot.models import IssueRecord, IssueState, ProcessResult
from autobot.state import StateStore


class FakeWatchTracker:
    def __init__(self, numbers: list[int]) -> None:
        self.numbers = numbers

    def list_actionable(self, repo: str) -> list[int]:
        return self.numbers


class SequencedWatchTracker:
    def __init__(self, results: list[list[int] | Exception]) -> None:
        self.results = results
        self.calls = 0

    def list_actionable(self, repo: str) -> list[int]:
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


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
        self.assertIn(
            "OPENAI_API_KEY, ANTHROPIC_API_KEY, or OPENROUTER_API_KEY is required",
            stderr.getvalue(),
        )

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

    def test_live_run_requires_openrouter_key_when_provider_is_openrouter(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {"GITHUB_TOKEN": "x", "LLM_PROVIDER": "openrouter"},
                clear=True,
            ),
            patch("autobot.cli._processor") as processor,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1"])

        self.assertEqual(code, 1)
        processor.assert_not_called()
        self.assertIn("OPENROUTER_API_KEY is required", stderr.getvalue())

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
        self.assertIn("LLM_PROVIDER must be openai, anthropic, or openrouter", stderr.getvalue())

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

    def test_live_run_rejects_invalid_llm_pricing_before_processor(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "GITHUB_TOKEN": "x",
                    "OPENAI_API_KEY": "x",
                    "REVIEW_INPUT_PRICE_PER_1K": "not-a-number",
                },
                clear=True,
            ),
            patch("autobot.cli._processor") as processor,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1"])

        self.assertEqual(code, 1)
        processor.assert_not_called()
        self.assertIn("REVIEW_INPUT_PRICE_PER_1K", stderr.getvalue())
        self.assertIn("must be numeric", stderr.getvalue())

    def test_live_run_rejects_dollar_budget_without_pricing_before_processor(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "GITHUB_TOKEN": "x",
                    "OPENAI_API_KEY": "x",
                    "MAX_ISSUE_DOLLARS": "1.00",
                },
                clear=True,
            ),
            patch("autobot.cli._processor") as processor,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1"])

        self.assertEqual(code, 1)
        processor.assert_not_called()
        self.assertIn("MAX_ISSUE_DOLLARS requires", stderr.getvalue())
        self.assertIn("TRIAGE_INPUT_PRICE_PER_1K", stderr.getvalue())

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
            patch("autobot.cli._ensure_live_prereqs"),
            patch("autobot.cli._processor", return_value=processor) as build_processor,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1"])

        self.assertEqual(code, 0)
        self.assertIn('"state": "pr_open"', stdout.getvalue())
        config = build_processor.call_args.args[0]
        self.assertEqual(config.review_models, ["gpt-4.1", "claude-sonnet-4-20250514"])
        self.assertIs(build_processor.call_args.kwargs["progress"], cli._print_run_progress)

    def test_run_quiet_suppresses_progress_callback(self) -> None:
        processor = FakeWatchProcessor()
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "x", "OPENAI_API_KEY": "x"}, clear=True),
            patch("autobot.cli._ensure_live_prereqs"),
            patch("autobot.cli._processor", return_value=processor) as build_processor,
            redirect_stdout(io.StringIO()),
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1", "--quiet"])

        self.assertEqual(code, 0)
        self.assertIsNone(build_processor.call_args.kwargs["progress"])

    def test_run_progress_prints_json_line_to_stderr(self) -> None:
        with redirect_stderr(io.StringIO()) as stderr:
            cli._print_run_progress(cli.WorkflowStep.TRIAGE)

        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["state"], "progress")
        self.assertEqual(payload["step"], "triage")
        self.assertEqual(payload["message"], "running triage LLM")

    def test_live_run_accepts_anthropic_key_without_openai_key(self) -> None:
        processor = FakeWatchProcessor()
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "x", "ANTHROPIC_API_KEY": "x"}, clear=True),
            patch("autobot.cli._ensure_live_prereqs"),
            patch("autobot.cli._processor", return_value=processor) as build_processor,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1"])

        self.assertEqual(code, 0)
        self.assertIn('"state": "pr_open"', stdout.getvalue())
        config = build_processor.call_args.args[0]
        self.assertEqual(config.implement_model, "claude-sonnet-4-20250514")

    def test_live_run_accepts_openrouter_key_without_direct_provider_key(self) -> None:
        processor = FakeWatchProcessor()
        with (
            patch.dict(
                "os.environ",
                {"GITHUB_TOKEN": "x", "OPENROUTER_API_KEY": "x"},
                clear=True,
            ),
            patch("autobot.cli._ensure_live_prereqs"),
            patch("autobot.cli._processor", return_value=processor) as build_processor,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1"])

        self.assertEqual(code, 0)
        self.assertIn('"state": "pr_open"', stdout.getvalue())
        config = build_processor.call_args.args[0]
        self.assertEqual(config.implement_model, "openai/gpt-4.1")

    def test_live_run_requires_pi_harness_provider_key_before_processor(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "GITHUB_TOKEN": "x",
                    "OPENAI_API_KEY": "x",
                    "IMPLEMENT_HARNESS": "pi",
                    "HARNESS_MODEL": "openrouter/google/gemini-2.5-pro",
                },
                clear=True,
            ),
            patch("autobot.cli._processor") as processor,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1"])

        self.assertEqual(code, 1)
        processor.assert_not_called()
        self.assertIn("OPENROUTER_API_KEY is required for Pi harness", stderr.getvalue())

    def test_live_run_preflight_failure_before_processor(self) -> None:
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "x", "OPENAI_API_KEY": "x"}, clear=True),
            patch(
                "autobot.cli.run_doctor",
                return_value=[
                    SimpleNamespace(name="docker", status="fail", message="docker missing")
                ],
            ) as run_doctor,
            patch("autobot.cli._processor") as processor,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["run", "--repo", "owner/repo", "--issue", "1"])

        self.assertEqual(code, 1)
        processor.assert_not_called()
        run_doctor.assert_called_once()
        self.assertFalse(run_doctor.call_args.kwargs["network"])
        self.assertIn("live prerequisite check failed", stderr.getvalue())
        self.assertIn("docker: docker missing", stderr.getvalue())

    def test_live_watch_fails_before_tracker_without_llm_key(self) -> None:
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "x"}, clear=True),
            patch("autobot.cli.GitHubIssueTracker") as tracker,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["watch", "--repo", "owner/repo", "--once"])

        self.assertEqual(code, 1)
        tracker.assert_not_called()
        self.assertIn(
            "OPENAI_API_KEY, ANTHROPIC_API_KEY, or OPENROUTER_API_KEY is required",
            stderr.getvalue(),
        )

    def test_live_watch_rejects_invalid_llm_pricing_before_tracker(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "GITHUB_TOKEN": "x",
                    "OPENAI_API_KEY": "x",
                    "IMPLEMENT_OUTPUT_PRICE_PER_1K": "not-a-number",
                },
                clear=True,
            ),
            patch("autobot.cli.GitHubIssueTracker") as tracker,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["watch", "--repo", "owner/repo", "--once"])

        self.assertEqual(code, 1)
        tracker.assert_not_called()
        self.assertIn("IMPLEMENT_OUTPUT_PRICE_PER_1K", stderr.getvalue())
        self.assertIn("must be numeric", stderr.getvalue())

    def test_live_watch_rejects_dollar_budget_without_pricing_before_tracker(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "GITHUB_TOKEN": "x",
                    "OPENAI_API_KEY": "x",
                    "MAX_ISSUE_DOLLARS": "1.00",
                },
                clear=True,
            ),
            patch("autobot.cli.GitHubIssueTracker") as tracker,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["watch", "--repo", "owner/repo", "--once"])

        self.assertEqual(code, 1)
        tracker.assert_not_called()
        self.assertIn("MAX_ISSUE_DOLLARS requires", stderr.getvalue())
        self.assertIn("TRIAGE_INPUT_PRICE_PER_1K", stderr.getvalue())

    def test_live_watch_preflight_failure_before_tracker(self) -> None:
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "x", "OPENAI_API_KEY": "x"}, clear=True),
            patch(
                "autobot.cli.run_doctor",
                return_value=[
                    SimpleNamespace(
                        name="git identity",
                        status="fail",
                        message="git user.name and user.email are required",
                    )
                ],
            ),
            patch("autobot.cli.GitHubIssueTracker") as tracker,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = cli.main(["watch", "--repo", "owner/repo", "--once"])

        self.assertEqual(code, 1)
        tracker.assert_not_called()
        self.assertIn("git identity", stderr.getvalue())
        self.assertIn("user.email", stderr.getvalue())

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

    def test_watch_uses_linear_tracker_when_configured(self) -> None:
        tracker = FakeWatchTracker([123])
        processor = FakeWatchProcessor()
        env = {
            "ISSUE_TRACKER": "linear",
            "LINEAR_API_KEY": "lin_api_secret",
            "LINEAR_TEAM_KEY": "ENG",
            "LINEAR_AGENT_LOGIN": "Autobot Linear",
        }

        with (
            patch.dict("os.environ", env, clear=True),
            patch("autobot.cli.LinearIssueTracker", return_value=tracker) as linear_tracker,
            patch("autobot.cli.GitHubIssueTracker") as github_tracker,
            patch("autobot.cli._processor", return_value=processor),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = cli.main(
                [
                    "watch",
                    "--repo",
                    "owner/repo",
                    "--once",
                    "--dry-run",
                    "--mock-llm",
                ]
            )

        self.assertEqual(code, 0)
        linear_tracker.assert_called_once_with("lin_api_secret", "Autobot Linear", "ENG")
        github_tracker.assert_not_called()
        self.assertEqual(processor.calls, [("owner/repo", 123)])
        self.assertEqual(json.loads(stdout.getvalue())["issue"], 123)

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

    def test_watch_once_reports_list_actionable_failure_as_json(self) -> None:
        token = "ghp_" + ("A" * 36)
        tracker = SequencedWatchTracker([RuntimeError(f"failed with {token}")])
        processor = FakeWatchProcessor()

        with (
            patch("autobot.cli.GitHubIssueTracker", return_value=tracker),
            patch("autobot.cli._processor", return_value=processor),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = cli.main(["watch", "--repo", "owner/repo", "--once", "--dry-run"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(processor.calls, [])
        self.assertEqual(payload["state"], "error")
        self.assertEqual(payload["phase"], "list_actionable")
        self.assertNotIn(token, payload["message"])
        self.assertIn("[redacted-secret]", payload["message"])

    def test_continuous_watch_retries_after_list_actionable_failure(self) -> None:
        token = "ghp_" + ("A" * 36)
        tracker = SequencedWatchTracker([RuntimeError(f"failed with {token}"), [2]])
        processor = FakeWatchProcessor()

        with (
            patch("autobot.cli.GitHubIssueTracker", return_value=tracker),
            patch("autobot.cli._processor", return_value=processor),
            patch("autobot.cli.time.sleep", side_effect=[None, KeyboardInterrupt]),
            redirect_stdout(io.StringIO()) as stdout,
            self.assertRaises(KeyboardInterrupt),
        ):
            cli.main(["watch", "--repo", "owner/repo", "--interval", "0", "--dry-run"])

        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(tracker.calls, 2)
        self.assertEqual(processor.calls, [("owner/repo", 2)])
        self.assertEqual(lines[0]["phase"], "list_actionable")
        self.assertEqual(lines[0]["state"], "error")
        self.assertNotIn(token, lines[0]["message"])
        self.assertEqual(lines[1]["state"], "pr_open")

    def test_state_clear_deletes_one_issue_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            store = StateStore(db)
            store.upsert(IssueRecord(repo="owner/repo", issue_number=7))

            with redirect_stdout(io.StringIO()) as stdout:
                code = cli.main(
                    [
                        "state",
                        "clear",
                        "--repo",
                        "owner/repo",
                        "--issue",
                        "7",
                        "--db",
                        str(db),
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["state"], "cleared")
            self.assertTrue(payload["deleted"])
            self.assertIsNone(store.get("owner/repo", 7))

    def test_state_clear_reports_missing_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"

            with redirect_stdout(io.StringIO()) as stdout:
                code = cli.main(
                    [
                        "state",
                        "clear",
                        "--repo",
                        "owner/repo",
                        "--issue",
                        "7",
                        "--db",
                        str(db),
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["state"], "not_found")
            self.assertFalse(payload["deleted"])

    def test_state_show_reports_record_with_latest_learning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            store = StateStore(db)
            record = IssueRecord(
                repo="owner/repo",
                issue_number=7,
                state=IssueState.PR_OPEN,
                branch="autobot/issue-7",
                pr_url="https://github.test/pull/7",
                review_rounds=1,
                files_touched=["README.md"],
                plan={"verification_commands": ["python -m unittest"]},
                cost={"dollars": 0.01},
                conversation={
                    "run_learnings": [
                        {
                            "at": "2026-06-08T00:00:00+00:00",
                            "state": "waiting",
                            "message": "waiting",
                            "observations": ["old"],
                            "learnings": ["old"],
                            "follow_up_actions": [],
                        },
                        {
                            "at": "2026-06-08T01:00:00+00:00",
                            "state": "pr_open",
                            "message": "opened draft pull request",
                            "observations": ["Draft PR reached after 1 review round(s)."],
                            "learnings": ["Keep review evidence together."],
                            "follow_up_actions": [],
                        },
                    ],
                    "pr_url": "https://github.test/pull/7",
                },
            )
            store.upsert(record)

            with redirect_stdout(io.StringIO()) as stdout:
                code = cli.main(
                    [
                        "state",
                        "show",
                        "--repo",
                        "owner/repo",
                        "--issue",
                        "7",
                        "--db",
                        str(db),
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["found"])
            self.assertEqual(payload["state"], "pr_open")
            self.assertEqual(payload["pr_url"], "https://github.test/pull/7")
            self.assertEqual(payload["verification_commands"], ["python -m unittest"])
            self.assertEqual(payload["latest_learning"]["state"], "pr_open")
            self.assertEqual(
                payload["latest_learning"]["learnings"],
                ["Keep review evidence together."],
            )

    def test_state_show_reports_missing_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"

            with redirect_stdout(io.StringIO()) as stdout:
                code = cli.main(
                    [
                        "state",
                        "show",
                        "--repo",
                        "owner/repo",
                        "--issue",
                        "7",
                        "--db",
                        str(db),
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["found"])
            self.assertEqual(payload["state"], "not_found")


if __name__ == "__main__":
    unittest.main()
