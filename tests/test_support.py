from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autobot.config import Config
from autobot.cost import CostLedger
from autobot.models import Usage
from autobot.sandbox import (
    LocalSandbox,
    SandboxError,
    run_verification,
    run_verification_allow_failure,
)
from autobot.scanner import (
    ensure_no_secret_like_values,
    find_secret_like_values,
    redact_secret_like_values,
)
from autobot.tests import (
    VerificationCommands,
    detect_verification_commands,
    merge_verification_commands,
)


class SupportTests(unittest.TestCase):
    def test_cost_ledger_totals_observed_usage(self) -> None:
        ledger = CostLedger()
        ledger.add(Usage("triage", "model", 10, 5, 0.001))
        ledger.add(Usage("review", "model", 20, 7, 0.002))

        self.assertEqual(ledger.input_tokens, 30)
        self.assertEqual(ledger.output_tokens, 12)
        self.assertEqual(ledger.dollars, 0.003)

    def test_secret_scanner_flags_private_key(self) -> None:
        findings = find_secret_like_values("-----BEGIN PRIVATE KEY-----\nabc\n")

        self.assertTrue(findings)

    def test_secret_scanner_flags_raw_github_token(self) -> None:
        token = "ghp_" + ("A" * 36)

        findings = find_secret_like_values(f"+GITHUB_TOKEN={token}\n")

        self.assertTrue(findings)

    def test_secret_scanner_flags_raw_openai_token(self) -> None:
        token = "sk-" + ("A" * 40)

        findings = find_secret_like_values(f"+OPENAI_API_KEY={token}\n")

        self.assertTrue(findings)

    def test_secret_redactor_removes_token_like_values(self) -> None:
        token = "ghp_" + ("A" * 36)

        redacted = redact_secret_like_values(f"git failed with {token}")

        self.assertNotIn(token, redacted)
        self.assertIn("[redacted-secret]", redacted)

    def test_secret_rejector_reports_count_without_echoing_value(self) -> None:
        token = "ghp_" + ("A" * 36)

        with self.assertRaises(RuntimeError) as raised:
            ensure_no_secret_like_values(f"use {token}", "prompt")

        self.assertIn("secret-like values found in prompt: 1 finding(s)", str(raised.exception))
        self.assertNotIn(token, str(raised.exception))

    def test_detects_python_verification_commands(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                "[tool.ruff]\n[tool.mypy]\n",
                encoding="utf-8",
            )

            commands = detect_verification_commands(root, None)

            self.assertEqual(commands.tests, ["python -m pytest"])
            self.assertEqual(
                commands.lint,
                ["python -m ruff check .", "python -m ruff format --check ."],
            )
            self.assertEqual(commands.types, ["python -m mypy ."])

    def test_detects_node_verification_commands(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                '{"scripts":{"test":"vitest","lint":"eslint .","typecheck":"tsc --noEmit"}}',
                encoding="utf-8",
            )

            commands = detect_verification_commands(root, None)

            self.assertEqual(commands.tests, ["npm test"])
        self.assertEqual(commands.lint, ["npm run lint"])
        self.assertEqual(commands.types, ["npm run typecheck"])

    def test_merge_keeps_authored_implementation_and_detected_tests(self) -> None:
        commands = merge_verification_commands(
            ["python -m pytest"],
            ["python -m pytest -q"],
            VerificationCommands(
                tests=["python -m unittest discover -s tests"],
                lint=["python -m ruff check ."],
            ),
        )

        self.assertEqual(
            commands,
            [
                "python -m pytest",
                "python -m pytest -q",
                "python -m unittest discover -s tests",
                "python -m ruff check .",
            ],
        )

    def test_baseline_verification_records_failures_without_raising(self) -> None:
        with TemporaryDirectory() as tmp:
            result = run_verification_allow_failure(LocalSandbox(Path(tmp)), ["false"], False)

        self.assertEqual(result["ok"], False)
        self.assertIn("$ false", result["output"])

    def test_verification_output_redacts_token_like_values(self) -> None:
        with TemporaryDirectory() as tmp:
            result = run_verification(
                LocalSandbox(Path(tmp)),
                ["python -c \"print('ghp_' + 'A' * 36)\""],
                False,
            )

        self.assertNotIn("ghp_" + ("A" * 36), result)
        self.assertIn("[redacted-secret]", result)

    def test_failing_verification_output_redacts_token_like_values(self) -> None:
        with TemporaryDirectory() as tmp:
            result = run_verification_allow_failure(
                LocalSandbox(Path(tmp)),
                ["python -c \"print('ghp_' + 'A' * 36); raise SystemExit(1)\""],
                False,
            )

        self.assertEqual(result["ok"], False)
        self.assertNotIn("ghp_" + ("A" * 36), result["output"])
        self.assertIn("[redacted-secret]", result["output"])

    def test_verification_rejects_secret_like_commands_before_execution(self) -> None:
        token = "ghp_" + ("A" * 36)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(SandboxError) as raised:
                run_verification(
                    LocalSandbox(root),
                    [f"printf '%s' '{token}' > leaked.txt"],
                    False,
                )

            self.assertFalse((root / "leaked.txt").exists())

        self.assertNotIn(token, str(raised.exception))
        self.assertIn("secret-like values found in verification commands", str(raised.exception))

    def test_allow_failure_rejects_secret_like_commands_before_execution(self) -> None:
        token = "sk-" + ("A" * 40)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(SandboxError) as raised:
                run_verification_allow_failure(
                    LocalSandbox(root),
                    [f"printf '%s' '{token}' > leaked.txt"],
                    False,
                )

            self.assertFalse((root / "leaked.txt").exists())

        self.assertNotIn(token, str(raised.exception))
        self.assertIn("secret-like values found in verification commands", str(raised.exception))

    def test_config_parses_review_model_list(self) -> None:
        with (
            TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"REVIEW_MODELS": "model-a, model-b"}, clear=True),
        ):
            config = Config.from_env(Path(tmp))

        self.assertEqual(config.review_models, ["model-a", "model-b"])

    def test_config_defaults_sandbox_network_to_none(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict("os.environ", {}, clear=True):
            config = Config.from_env(Path(tmp))

        self.assertEqual(config.sandbox_network, "none")

    def test_config_rejects_zero_review_rounds(self) -> None:
        with (
            TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"MAX_REVIEW_ROUNDS": "0"}, clear=True),
            self.assertRaisesRegex(ValueError, "MAX_REVIEW_ROUNDS must be between 1 and 3"),
        ):
            Config.from_env(Path(tmp))

    def test_config_rejects_more_than_three_review_rounds(self) -> None:
        with (
            TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"MAX_REVIEW_ROUNDS": "4"}, clear=True),
            self.assertRaisesRegex(ValueError, "MAX_REVIEW_ROUNDS must be between 1 and 3"),
        ):
            Config.from_env(Path(tmp))


if __name__ == "__main__":
    unittest.main()
