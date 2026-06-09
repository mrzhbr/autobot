from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from autobot.config import Config
from autobot.doctor import doctor_ok, run_doctor
from autobot.models import Issue


def passing_command(command, capture_output, text, check, timeout):
    return SimpleNamespace(returncode=0, stdout=f"{command[0]} ok\n", stderr="")


def missing_git_identity_command(command, capture_output, text, check, timeout):
    if command[:3] == ["git", "config", "--get"]:
        return SimpleNamespace(returncode=1, stdout="", stderr="")
    return passing_command(command, capture_output, text, check, timeout)


def missing_docker_command(command, capture_output, text, check, timeout):
    if command[0] == "docker":
        raise OSError("docker missing")
    return passing_command(command, capture_output, text, check, timeout)


def missing_docker_daemon_command(command, capture_output, text, check, timeout):
    if command[:2] == ["docker", "info"]:
        return SimpleNamespace(returncode=1, stdout="", stderr="Cannot connect to Docker daemon")
    return passing_command(command, capture_output, text, check, timeout)


def leaking_git_command(command, capture_output, text, check, timeout):
    token = "ghp_" + ("A" * 36)
    if command == ["git", "--version"]:
        return SimpleNamespace(returncode=1, stdout="", stderr=f"fatal: {token}")
    return passing_command(command, capture_output, text, check, timeout)


def leaking_git_identity_command(command, capture_output, text, check, timeout):
    token = "ghp_" + ("A" * 36)
    if command == ["git", "config", "--get", "user.name"]:
        return SimpleNamespace(returncode=0, stdout=f"{token}\n", stderr="")
    if command == ["git", "config", "--get", "user.email"]:
        return SimpleNamespace(returncode=0, stdout="bot@example.invalid\n", stderr="")
    return passing_command(command, capture_output, text, check, timeout)


def pi_missing_command(command, capture_output, text, check, timeout):
    if command[:3] == ["docker", "run", "--rm"]:
        return SimpleNamespace(returncode=127, stdout="", stderr="sh: pi: not found")
    return passing_command(command, capture_output, text, check, timeout)


class RecordingCommandRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command, capture_output, text, check, timeout):
        self.commands.append(command)
        if command[:3] == ["docker", "run", "--rm"]:
            return SimpleNamespace(returncode=0, stdout="0.75.5\n", stderr="")
        return passing_command(command, capture_output, text, check, timeout)


class FakeTracker:
    def __init__(self, token: str | None, agent_login: str | None) -> None:
        self.token = token
        self.agent_login = agent_login

    def get(self, repo: str, issue: int) -> Issue:
        return Issue(repo, issue, "Title", "Body", "alice", [])


class FailingTracker:
    def __init__(self, token: str | None, agent_login: str | None) -> None:
        self.token = token
        self.agent_login = agent_login

    def get(self, repo: str, issue: int) -> Issue:
        raise RuntimeError(f"failed with {self.token}")


class DoctorTests(unittest.TestCase):
    def test_live_doctor_fails_without_required_credentials(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict("os.environ", {}, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            self.assertFalse(doctor_ok(checks))
            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["github token"].status, "fail")
            self.assertEqual(by_name["llm key"].status, "fail")

    def test_dry_run_doctor_skips_live_credentials(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict("os.environ", {}, clear=True):
            config = Config.from_env(Path(tmp), dry_run=True, mock_llm=True)

            checks = run_doctor(config, command_runner=missing_docker_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertTrue(doctor_ok(checks))
            self.assertEqual(by_name["docker"].status, "skip")
            self.assertEqual(by_name["github token"].status, "skip")
            self.assertEqual(by_name["git identity"].status, "skip")
            self.assertEqual(by_name["llm key"].status, "skip")
            self.assertEqual(by_name["sandbox network"].status, "skip")
            self.assertEqual(by_name["issue readable"].status, "skip")

    def test_live_doctor_fails_when_git_identity_is_missing(self) -> None:
        env = {"GITHUB_TOKEN": "x", "OPENAI_API_KEY": "x"}
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=missing_git_identity_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertFalse(doctor_ok(checks))
            self.assertEqual(by_name["git identity"].status, "fail")
            self.assertIn("user.name", by_name["git identity"].message)

    def test_live_doctor_redacts_command_check_output(self) -> None:
        token = "ghp_" + ("A" * 36)
        env = {"GITHUB_TOKEN": "x", "OPENAI_API_KEY": "x"}
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=leaking_git_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["git"].status, "fail")
            self.assertNotIn(token, by_name["git"].message)
            self.assertIn("[redacted-secret]", by_name["git"].message)

    def test_live_doctor_redacts_all_check_messages(self) -> None:
        token = "ghp_" + ("A" * 36)
        env = {"GITHUB_TOKEN": "x", "OPENAI_API_KEY": "x"}
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(
                config,
                command_runner=leaking_git_identity_command,
                network=False,
            )

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["git identity"].status, "pass")
            self.assertNotIn(token, by_name["git identity"].message)
            self.assertIn("[redacted-secret]", by_name["git identity"].message)

    def test_live_doctor_fails_when_docker_is_missing(self) -> None:
        env = {"GITHUB_TOKEN": "x", "OPENAI_API_KEY": "x"}
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=missing_docker_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertFalse(doctor_ok(checks))
            self.assertEqual(by_name["docker"].status, "fail")
            self.assertIn("docker missing", by_name["docker"].message)

    def test_live_doctor_fails_when_docker_daemon_is_unavailable(self) -> None:
        env = {"GITHUB_TOKEN": "x", "OPENAI_API_KEY": "x"}
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(
                config,
                command_runner=missing_docker_daemon_command,
                network=False,
            )

            by_name = {check.name: check for check in checks}
            self.assertFalse(doctor_ok(checks))
            self.assertEqual(by_name["docker"].status, "fail")
            self.assertIn("daemon unavailable", by_name["docker"].message)
            self.assertIn("Cannot connect", by_name["docker"].message)

    def test_live_doctor_rejects_unknown_llm_provider(self) -> None:
        env = {"GITHUB_TOKEN": "x", "OPENAI_API_KEY": "x", "LLM_PROVIDER": "bogus"}
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertFalse(doctor_ok(checks))
            self.assertEqual(by_name["llm key"].status, "fail")
            self.assertIn(
                "LLM_PROVIDER must be openai, anthropic, or openrouter",
                by_name["llm key"].message,
            )

    def test_live_doctor_uses_anthropic_default_model_for_anthropic_key(self) -> None:
        env = {"GITHUB_TOKEN": "x", "ANTHROPIC_API_KEY": "x"}
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertTrue(doctor_ok(checks))
            self.assertEqual(by_name["llm key"].message, "ANTHROPIC_API_KEY is set")
            self.assertEqual(by_name["triage model"].message, "claude-sonnet-4-20250514")

    def test_live_doctor_uses_openrouter_default_model_for_openrouter_key(self) -> None:
        env = {"GITHUB_TOKEN": "x", "OPENROUTER_API_KEY": "x"}
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertTrue(doctor_ok(checks))
            self.assertEqual(by_name["llm key"].message, "OPENROUTER_API_KEY is set")
            self.assertEqual(by_name["triage model"].message, "openai/gpt-4.1")
            self.assertIn("openrouter", by_name["llm model/provider"].message)

    def test_live_doctor_fails_openrouter_model_when_key_is_missing(self) -> None:
        env = {
            "GITHUB_TOKEN": "x",
            "OPENAI_API_KEY": "x",
            "REVIEW_MODELS": "gpt-4.1,openrouter/google/gemini-2.5-pro",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertFalse(doctor_ok(checks))
            self.assertEqual(by_name["llm model/provider"].status, "fail")
            self.assertIn("OPENROUTER_API_KEY", by_name["llm model/provider"].message)
            self.assertIn("openrouter/google/gemini-2.5-pro", by_name["llm model/provider"].message)

    def test_live_doctor_fails_pi_harness_without_sandbox_network_egress(self) -> None:
        env = {
            "GITHUB_TOKEN": "x",
            "OPENROUTER_API_KEY": "x",
            "IMPLEMENT_HARNESS": "pi",
            "HARNESS_MODEL": "openrouter/google/gemini-2.5-pro",
            "SANDBOX_NETWORK": "none",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertFalse(doctor_ok(checks))
            self.assertEqual(by_name["implementation harness"].status, "fail")
            self.assertIn("requires SANDBOX_NETWORK", by_name["implementation harness"].message)

    def test_live_doctor_accepts_pi_harness_with_provider_key_and_network(self) -> None:
        env = {
            "GITHUB_TOKEN": "x",
            "OPENROUTER_API_KEY": "x",
            "IMPLEMENT_HARNESS": "pi",
            "HARNESS_MODEL": "openrouter/google/gemini-2.5-pro",
            "SANDBOX_NETWORK": "bridge",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))
            runner = RecordingCommandRunner()

            checks = run_doctor(config, command_runner=runner, network=False)

            by_name = {check.name: check for check in checks}
            self.assertTrue(doctor_ok(checks))
            self.assertEqual(by_name["implementation harness"].status, "pass")
            self.assertIn("pi 0.75.5 using openrouter", by_name["implementation harness"].message)
            docker_run = next(
                command for command in runner.commands if command[:3] == ["docker", "run", "--rm"]
            )
            self.assertIn("python:3.12-slim", docker_run)
            self.assertIn("PI_CODING_AGENT_DIR=/tmp/autobot-pi-agent", docker_run[-1])

    def test_live_doctor_accepts_enabled_pi_planner_with_provider_key_and_network(self) -> None:
        env = {
            "GITHUB_TOKEN": "x",
            "OPENROUTER_API_KEY": "x",
            "PLANNER_ENABLED": "1",
            "PLANNER_LLM_PROVIDER": "openrouter",
            "PLANNER_MODEL": "openrouter/anthropic/claude-opus-4.8",
            "SANDBOX_NETWORK": "bridge",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))
            runner = RecordingCommandRunner()

            checks = run_doctor(config, command_runner=runner, network=False)

            by_name = {check.name: check for check in checks}
            self.assertTrue(doctor_ok(checks))
            self.assertEqual(by_name["planner model"].status, "pass")
            self.assertEqual(by_name["planner harness"].status, "pass")
            self.assertIn("claude-opus-4.8", by_name["planner harness"].message)

    def test_live_doctor_fails_enabled_pi_planner_without_sandbox_network_egress(self) -> None:
        env = {
            "GITHUB_TOKEN": "x",
            "OPENROUTER_API_KEY": "x",
            "PLANNER_ENABLED": "1",
            "PLANNER_LLM_PROVIDER": "openrouter",
            "PLANNER_MODEL": "openrouter/anthropic/claude-opus-4.8",
            "SANDBOX_NETWORK": "none",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertFalse(doctor_ok(checks))
            self.assertEqual(by_name["planner harness"].status, "fail")
            self.assertIn("requires SANDBOX_NETWORK", by_name["planner harness"].message)

    def test_live_doctor_fails_pi_harness_when_image_lacks_pi(self) -> None:
        env = {
            "GITHUB_TOKEN": "x",
            "OPENROUTER_API_KEY": "x",
            "IMPLEMENT_HARNESS": "pi",
            "HARNESS_MODEL": "openrouter/google/gemini-2.5-pro",
            "SANDBOX_NETWORK": "bridge",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=pi_missing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertFalse(doctor_ok(checks))
            self.assertEqual(by_name["implementation harness"].status, "fail")
            self.assertIn("Pi CLI is not available", by_name["implementation harness"].message)

    def test_live_doctor_fails_review_model_when_matching_key_is_missing(self) -> None:
        env = {
            "GITHUB_TOKEN": "x",
            "OPENAI_API_KEY": "x",
            "REVIEW_MODELS": "gpt-4.1,claude-sonnet-4-20250514",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertFalse(doctor_ok(checks))
            self.assertEqual(by_name["llm model/provider"].status, "fail")
            self.assertIn("ANTHROPIC_API_KEY", by_name["llm model/provider"].message)
            self.assertIn("claude-sonnet-4-20250514", by_name["llm model/provider"].message)

    def test_live_doctor_accepts_mixed_review_models_when_keys_exist(self) -> None:
        env = {
            "GITHUB_TOKEN": "x",
            "OPENAI_API_KEY": "x",
            "ANTHROPIC_API_KEY": "x",
            "REVIEW_MODELS": "gpt-4.1,claude-sonnet-4-20250514",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertTrue(doctor_ok(checks))
            self.assertEqual(by_name["llm model/provider"].status, "pass")
            self.assertIn("openai, anthropic", by_name["llm model/provider"].message)

    def test_live_doctor_warns_when_llm_pricing_is_missing(self) -> None:
        env = {"GITHUB_TOKEN": "x", "OPENAI_API_KEY": "x"}
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["llm pricing"].status, "warn")
            self.assertIn("not configured", by_name["llm pricing"].message)
            self.assertIn("TRIAGE_INPUT_PRICE_PER_1K", by_name["llm pricing"].message)
            self.assertIn("REVIEW_OUTPUT_PRICE_PER_1K", by_name["llm pricing"].message)

    def test_live_doctor_fails_when_dollar_budget_lacks_pricing(self) -> None:
        env = {
            "GITHUB_TOKEN": "x",
            "OPENAI_API_KEY": "x",
            "MAX_ISSUE_DOLLARS": "1.00",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertFalse(doctor_ok(checks))
            self.assertEqual(by_name["llm pricing"].status, "fail")
            self.assertIn("MAX_ISSUE_DOLLARS requires", by_name["llm pricing"].message)
            self.assertIn("TRIAGE_INPUT_PRICE_PER_1K", by_name["llm pricing"].message)

    def test_live_doctor_passes_when_llm_pricing_is_configured(self) -> None:
        env = {
            "GITHUB_TOKEN": "x",
            "OPENAI_API_KEY": "x",
            "TRIAGE_INPUT_PRICE_PER_1K": "0.001",
            "TRIAGE_OUTPUT_PRICE_PER_1K": "0.002",
            "IMPLEMENT_INPUT_PRICE_PER_1K": "0.003",
            "IMPLEMENT_OUTPUT_PRICE_PER_1K": "0.004",
            "REVIEW_INPUT_PRICE_PER_1K": "0.005",
            "REVIEW_OUTPUT_PRICE_PER_1K": "0.006",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["llm pricing"].status, "pass")
            self.assertIn("test", by_name["llm pricing"].message)

    def test_live_doctor_fails_when_llm_pricing_is_not_numeric(self) -> None:
        env = {
            "GITHUB_TOKEN": "x",
            "OPENAI_API_KEY": "x",
            "TRIAGE_INPUT_PRICE_PER_1K": "0.001",
            "TRIAGE_OUTPUT_PRICE_PER_1K": "0.002",
            "IMPLEMENT_INPUT_PRICE_PER_1K": "0.003",
            "IMPLEMENT_OUTPUT_PRICE_PER_1K": "0.004",
            "REVIEW_INPUT_PRICE_PER_1K": "0.005",
            "REVIEW_OUTPUT_PRICE_PER_1K": "not-a-number",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertFalse(doctor_ok(checks))
            self.assertEqual(by_name["llm pricing"].status, "fail")
            self.assertIn("REVIEW_OUTPUT_PRICE_PER_1K", by_name["llm pricing"].message)

    def test_live_doctor_warns_when_sandbox_network_allows_egress(self) -> None:
        env = {
            "GITHUB_TOKEN": "x",
            "OPENAI_API_KEY": "x",
            "SANDBOX_NETWORK": "bridge",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["sandbox network"].status, "warn")
            self.assertIn("egress", by_name["sandbox network"].message)

    def test_live_doctor_fails_when_sandbox_network_is_empty(self) -> None:
        env = {
            "GITHUB_TOKEN": "x",
            "OPENAI_API_KEY": "x",
            "SANDBOX_NETWORK": "",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertFalse(doctor_ok(checks))
            self.assertEqual(by_name["sandbox network"].status, "fail")
            self.assertIn("SANDBOX_NETWORK must not be empty", by_name["sandbox network"].message)

    def test_live_doctor_reports_configured_sandbox_setup_command(self) -> None:
        env = {
            "GITHUB_TOKEN": "x",
            "OPENAI_API_KEY": "x",
            "SANDBOX_SETUP_COMMAND": "python -m pip install -e .",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["sandbox setup"].status, "pass")
            self.assertEqual(by_name["sandbox setup"].message, "python -m pip install -e .")

    def test_live_doctor_warns_when_sandbox_setup_is_auto_detected_later(self) -> None:
        env = {"GITHUB_TOKEN": "x", "OPENAI_API_KEY": "x"}
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["sandbox setup"].status, "warn")
            self.assertIn("auto-detected after clone", by_name["sandbox setup"].message)

    def test_live_doctor_rejects_secret_like_sandbox_setup_command(self) -> None:
        token = "ghp_" + ("A" * 36)
        env = {
            "GITHUB_TOKEN": "x",
            "OPENAI_API_KEY": "x",
            "SANDBOX_SETUP_COMMAND": f"echo {token}",
        }
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["sandbox setup"].status, "fail")
            self.assertNotIn(token, by_name["sandbox setup"].message)
            self.assertIn(
                "secret-like values found in sandbox setup command",
                by_name["sandbox setup"].message,
            )

    def test_issue_readability_uses_tracker_when_requested(self) -> None:
        with (
            TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"GITHUB_TOKEN": "x"}, clear=True),
        ):
            config = Config.from_env(Path(tmp), dry_run=True, mock_llm=True)

            checks = run_doctor(
                config,
                repo="owner/repo",
                issue=7,
                command_runner=passing_command,
                tracker_factory=FakeTracker,
            )

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["issue readable"].status, "pass")
            self.assertEqual(by_name["issue readable"].message, "owner/repo#7")

    def test_issue_readability_redacts_tracker_errors(self) -> None:
        token = "ghp_" + ("A" * 36)
        with (
            TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"GITHUB_TOKEN": token}, clear=True),
        ):
            config = Config.from_env(Path(tmp), dry_run=True, mock_llm=True)

            checks = run_doctor(
                config,
                repo="owner/repo",
                issue=7,
                command_runner=passing_command,
                tracker_factory=FailingTracker,
            )

            by_name = {check.name: check for check in checks}
            self.assertEqual(by_name["issue readable"].status, "fail")
            self.assertNotIn(token, by_name["issue readable"].message)
            self.assertIn("[redacted-secret]", by_name["issue readable"].message)


if __name__ == "__main__":
    unittest.main()
