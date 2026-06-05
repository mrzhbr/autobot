from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autobot.config import Config
from autobot.cost import CostLedger
from autobot.models import Usage
from autobot.sandbox import LocalSandbox, run_verification_allow_failure
from autobot.scanner import find_secret_like_values
from autobot.tests import detect_verification_commands


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

    def test_baseline_verification_records_failures_without_raising(self) -> None:
        with TemporaryDirectory() as tmp:
            result = run_verification_allow_failure(LocalSandbox(Path(tmp)), ["false"], False)

        self.assertEqual(result["ok"], False)
        self.assertIn("$ false", result["output"])

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


if __name__ == "__main__":
    unittest.main()
