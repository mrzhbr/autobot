from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autobot import cli
from autobot.config import Config
from autobot.eval_harness import (
    EvalCost,
    EvalExpectations,
    EvalModelRef,
    EvalScore,
    EvalVerificationResult,
    HarnessEvalResult,
    append_result,
    load_fixture,
    run_harness_eval,
    score_eval,
)
from autobot.harness import HarnessResult
from autobot.models import FileChange, Usage

ROOT = Path(__file__).resolve().parents[1]


class EvalHarnessTests(unittest.TestCase):
    def test_load_fixture_reads_issue_expectations_and_mock_runs(self) -> None:
        fixture = load_fixture(ROOT, "python-add")

        self.assertEqual(fixture.name, "python-add")
        self.assertEqual(fixture.issue.title, "Fix calculator add")
        self.assertEqual(fixture.expectations.changed_files, ["app/calculator.py"])
        self.assertIn("legacy", fixture.mock_runs)
        self.assertIn("pi", fixture.mock_runs)

    def test_score_eval_reports_verification_and_pattern_failures(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "app").mkdir()
            (repo / "app" / "calculator.py").write_text(
                "def add(left, right):\n    return left - right\n",
                encoding="utf-8",
            )
            expectations = EvalExpectations.model_validate(
                {
                    "changed_files": ["app/calculator.py", "tests/test_calculator.py"],
                    "allowed_patterns": [
                        {"path": "app/calculator.py", "pattern": "return left + right"}
                    ],
                    "forbidden_patterns": [
                        {"path": "app/calculator.py", "pattern": "return left - right"}
                    ],
                }
            )

            score = score_eval(repo, expectations, ["app/calculator.py"], verification_ok=False)

        self.assertFalse(score.passed)
        self.assertIn("verification failed", score.reasons)
        self.assertIn("missing expected files: tests/test_calculator.py", score.reasons)
        self.assertIn(
            "missing required pattern in app/calculator.py: return left + right",
            score.reasons,
        )
        self.assertIn(
            "forbidden pattern found in app/calculator.py: return left - right",
            score.reasons,
        )

    def test_run_harness_eval_pi_mock_passes_and_persists_jsonl(self) -> None:
        with (
            TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"PATH": os.environ["PATH"]}, clear=True),
        ):
            output_dir = Path(tmp) / "evals"
            config = Config.from_env(ROOT, dry_run=True, mock_llm=True)

            result = run_harness_eval(
                ROOT,
                "welcome-copy",
                "pi",
                mock_llm=True,
                config=config,
                output_dir=output_dir,
            )

            rows = (output_dir / "harness-results.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertTrue(result.score.passed)
        self.assertEqual(
            result.files_touched,
            ["notifications/formatters.py", "notifications/messages.py"],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0])["fixture_name"], "welcome-copy")

    def test_run_harness_eval_legacy_mock_fails_ambiguous_fixture(self) -> None:
        with (
            TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"PATH": os.environ["PATH"]}, clear=True),
        ):
            config = Config.from_env(ROOT, dry_run=True, mock_llm=True)

            result = run_harness_eval(
                ROOT,
                "welcome-copy",
                "legacy",
                mock_llm=True,
                config=config,
                output_dir=Path(tmp) / "evals",
            )

        self.assertFalse(result.score.passed)
        self.assertFalse(result.verification.ok)
        self.assertIn("missing expected files: notifications/messages.py", result.score.reasons)

    def test_run_harness_eval_live_uses_provider_harness_without_mock_fixture(self) -> None:
        adapter = FakeLiveAdapter()
        with (
            TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"PATH": os.environ["PATH"]}, clear=True),
            patch("autobot.eval_live.build_llm", return_value=object()) as build_llm,
            patch("autobot.eval_live.build_harness_adapter", return_value=adapter),
        ):
            output_dir = Path(tmp) / "evals"
            config = Config.from_env(ROOT)

            result = run_harness_eval(
                ROOT,
                "python-add",
                "legacy",
                mock_llm=False,
                config=config,
                output_dir=output_dir,
            )

            rows = (output_dir / "harness-results.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertTrue(result.score.passed)
        self.assertEqual(result.mode, "live")
        self.assertEqual(result.cost.input_tokens, 7)
        self.assertEqual(result.files_touched, ["app/calculator.py"])
        self.assertEqual(adapter.session.tasks, ["implement"])
        self.assertEqual(len(rows), 1)
        build_llm.assert_called_once()

    def test_append_result_writes_one_json_line(self) -> None:
        result = _result()
        with TemporaryDirectory() as tmp:
            path = append_result(Path(tmp), result)
            rows = path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0])
        self.assertEqual(payload["fixture_name"], "python-add")
        self.assertEqual(payload["score"]["passed"], True)

    def test_cli_eval_harness_prints_result_and_uses_mock_mode(self) -> None:
        result = _result()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("autobot.eval_harness.run_harness_eval", return_value=result) as run_eval,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = cli.main(
                ["eval-harness", "--fixture", "python-add", "--harness", "pi", "--mock-llm"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["fixture_name"], "python-add")
        self.assertEqual(run_eval.call_args.args[1:3], ("python-add", "pi"))
        self.assertTrue(run_eval.call_args.kwargs["mock_llm"])


def _result() -> HarnessEvalResult:
    return HarnessEvalResult(
        fixture_name="python-add",
        harness="pi",
        mode="mock",
        planner_enabled=False,
        planner=EvalModelRef(),
        implement=EvalModelRef(provider=None, model="gpt-4.1"),
        reviewers=[],
        state="passed",
        result="pass",
        files_touched=["app/calculator.py"],
        verification=EvalVerificationResult(
            commands=["python3 -m unittest discover -s tests"],
            ok=True,
            output_summary="OK",
        ),
        review_rounds=1,
        blockers=[],
        cost=EvalCost(input_tokens=1, output_tokens=1, dollars=0.0),
        wall_seconds=0.01,
        transcript_path="/tmp/transcript.txt",
        log_paths=["/tmp/transcript.txt"],
        score=EvalScore(passed=True, reasons=["verification passed"]),
    )


class FakeLiveAdapter:
    def __init__(self) -> None:
        self.session = FakeLiveSession()

    def start(self, repo_dir, sandbox=None, dry_run=False):
        return self.session


class FakeLiveSession:
    def __init__(self) -> None:
        self.tasks: list[str] = []
        self.closed = False

    def plan(self, task):
        raise AssertionError("planner should not run in this test")

    def run(self, task):
        self.tasks.append(task.kind.value)
        return HarnessResult(
            plan=["Fix arithmetic."],
            changes=[
                FileChange(
                    "app/calculator.py",
                    "def add(left: int, right: int) -> int:\n    return left + right\n",
                )
            ],
            test_commands=[],
            usage=Usage("implement", "fake-live", 7, 3, 0.02),
        )

    def close(self) -> None:
        self.closed = True


if __name__ == "__main__":
    unittest.main()
