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


class FakeTracker:
    def __init__(self, token: str | None, agent_login: str | None) -> None:
        self.token = token
        self.agent_login = agent_login

    def get(self, repo: str, issue: int) -> Issue:
        return Issue(repo, issue, "Title", "Body", "alice", [])


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
            self.assertEqual(by_name["llm key"].status, "skip")
            self.assertEqual(by_name["issue readable"].status, "skip")

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


if __name__ == "__main__":
    unittest.main()
