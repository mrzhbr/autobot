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

            checks = run_doctor(config, command_runner=passing_command, network=False)

            by_name = {check.name: check for check in checks}
            self.assertTrue(doctor_ok(checks))
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
