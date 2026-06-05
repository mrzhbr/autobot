from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autobot.models import FileChange
from autobot.sandbox import DockerSandbox, LocalSandbox


class SandboxTests(unittest.TestCase):
    def test_docker_run_uses_configured_network_and_work_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

            with patch("autobot.sandbox.subprocess.run", return_value=completed) as run:
                output = DockerSandbox(repo, "python:3.12-slim", network="none").run(
                    "python -m pytest", timeout=123
                )

        command = run.call_args.args[0]
        self.assertEqual(output, "ok")
        self.assertEqual(
            command,
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{repo}:/work",
                "-w",
                "/work",
                "python:3.12-slim",
                "sh",
                "-lc",
                "python -m pytest",
            ],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 123)
        self.assertEqual(run.call_args.kwargs["check"], False)

    def test_docker_apply_changes_mounts_payload_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("autobot.sandbox.subprocess.run", return_value=completed) as run:
                DockerSandbox(repo, "python:3.12-slim").apply_changes(
                    [FileChange("README.md", "# Demo\n")]
                )

            payload = json.loads((repo.parent / "changes.json").read_text(encoding="utf-8"))

        command = run.call_args.args[0]
        self.assertEqual(payload[0]["path"], "README.md")
        self.assertIn(f"{repo.parent}:/changes:ro", command)
        self.assertEqual(command[command.index("--network") + 1], "none")

    def test_local_sandbox_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()

            with self.assertRaises(ValueError):
                LocalSandbox(root).apply_changes([FileChange("../escape.txt", "no\n")])

            self.assertFalse((root.parent / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
