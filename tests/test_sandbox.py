from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autobot.models import FileChange
from autobot.sandbox import DockerSandbox, LocalSandbox, SandboxError, detect_setup_command


class SandboxTests(unittest.TestCase):
    def test_detect_setup_command_uses_common_stack_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python_repo = root / "python"
            node_repo = root / "node"
            go_repo = root / "go"
            rust_repo = root / "rust"
            for repo in (python_repo, node_repo, go_repo, rust_repo):
                repo.mkdir()
            (python_repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (python_repo / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
            (node_repo / "package.json").write_text("{}", encoding="utf-8")
            (node_repo / "package-lock.json").write_text("{}", encoding="utf-8")
            (go_repo / "go.mod").write_text("module example.test/demo\n", encoding="utf-8")
            (rust_repo / "Cargo.toml").write_text("[package]\n", encoding="utf-8")

            python_setup = (
                "python -m venv .autobot-bootstrap"
                " && .autobot-bootstrap/bin/python -m pip install --upgrade pip uv"
                " && UV_PROJECT_ENVIRONMENT=.venv UV_PYTHON_PREFERENCE=managed "
                ".autobot-bootstrap/bin/uv sync --all-extras --dev"
            )
            self.assertEqual(
                detect_setup_command(python_repo, None),
                python_setup,
            )
            self.assertEqual(detect_setup_command(node_repo, None), "npm ci")
            self.assertEqual(detect_setup_command(go_repo, None), "go mod download")
            self.assertEqual(detect_setup_command(rust_repo, None), "cargo fetch")

    def test_detect_setup_command_uses_declared_python_dev_extra(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text(
                "[project]\n"
                "requires-python = '>=3.12'\n"
                "[project.optional-dependencies]\n"
                "dev = ['pytest']\n",
                encoding="utf-8",
            )

            self.assertEqual(
                detect_setup_command(repo, None),
                "python -m venv .autobot-bootstrap"
                " && .autobot-bootstrap/bin/python -m pip install --upgrade pip uv"
                " && UV_PROJECT_ENVIRONMENT=.venv UV_PYTHON_PREFERENCE=managed "
                ".autobot-bootstrap/bin/uv sync --python 3.12 --all-extras --dev",
            )

    def test_detect_setup_command_uses_setup_cfg_dev_extra(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "setup.cfg").write_text(
                "[metadata]\nname = demo\n[options.extras_require]\ndev =\n    pytest\n",
                encoding="utf-8",
            )

            self.assertEqual(
                detect_setup_command(repo, None),
                "python -m venv .venv"
                " && .venv/bin/python -m pip install --upgrade pip"
                ' && .venv/bin/python -m pip install -e ".[dev]"',
            )

    def test_detect_setup_command_uses_uv_lock_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text(
                "[project]\nrequires-python = '>=3.12'\n",
                encoding="utf-8",
            )
            (repo / "uv.lock").write_text("", encoding="utf-8")

            self.assertEqual(
                detect_setup_command(repo, None),
                "python -m venv .autobot-bootstrap"
                " && .autobot-bootstrap/bin/python -m pip install --upgrade pip uv"
                " && UV_PROJECT_ENVIRONMENT=.venv UV_PYTHON_PREFERENCE=managed "
                ".autobot-bootstrap/bin/uv sync --python 3.12 --frozen --all-extras --dev",
            )

    def test_detect_setup_command_uses_ruff_python_target_when_no_requires_python(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text(
                "[project]\n[tool.ruff]\ntarget-version = 'py313'\n",
                encoding="utf-8",
            )

            self.assertEqual(
                detect_setup_command(repo, None),
                "python -m venv .autobot-bootstrap"
                " && .autobot-bootstrap/bin/python -m pip install --upgrade pip uv"
                " && UV_PROJECT_ENVIRONMENT=.venv UV_PYTHON_PREFERENCE=managed "
                ".autobot-bootstrap/bin/uv sync --python 3.13 --all-extras --dev",
            )

    def test_detect_setup_command_prefers_explicit_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "package.json").write_text("{}", encoding="utf-8")

            self.assertEqual(detect_setup_command(repo, "make bootstrap"), "make bootstrap")

    def test_docker_run_uses_configured_network_and_work_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            started = SimpleNamespace(returncode=0, stdout="container-1\n", stderr="")
            completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

            with patch("autobot.sandbox.subprocess.run", side_effect=[started, completed]) as run:
                output = DockerSandbox(repo, "python:3.12-slim", network="none").run(
                    "python -m pytest", timeout=123
                )

        start_command = run.call_args_list[0].args[0]
        exec_command = run.call_args_list[1].args[0]
        self.assertEqual(output, "ok")
        self.assertEqual(
            start_command,
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{repo}:/work",
                "-v",
                f"{repo.parent}:/changes:ro",
                "-w",
                "/work",
                "python:3.12-slim",
                "sh",
                "-c",
                "while true; do sleep 3600; done",
            ],
        )
        self.assertEqual(
            exec_command,
            [
                "docker",
                "exec",
                "-w",
                "/work",
                "-e",
                "PATH=/work/.venv/bin:/work/.autobot-bootstrap/bin:"
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "-e",
                "UV_PROJECT_ENVIRONMENT=/work/.venv",
                "container-1",
                "sh",
                "-c",
                "python -m pytest",
            ],
        )
        self.assertEqual(run.call_args_list[1].kwargs["timeout"], 123)
        self.assertEqual(run.call_args_list[1].kwargs["check"], False)

    def test_docker_copy_backend_does_not_bind_mount_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            started = SimpleNamespace(returncode=0, stdout="container-1\n", stderr="")
            mkdir = SimpleNamespace(returncode=0, stdout="", stderr="")
            copied = SimpleNamespace(returncode=0, stdout="", stderr="")
            safe_dir = SimpleNamespace(returncode=0, stdout="", stderr="")
            completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="warn\n")

            with patch(
                "autobot.sandbox.subprocess.run",
                side_effect=[started, mkdir, copied, safe_dir, completed],
            ) as run:
                output = DockerSandbox(repo, "python:3.12-slim", mode="copy").run("pytest")

        start_command = run.call_args_list[0].args[0]
        mkdir_command = run.call_args_list[1].args[0]
        copy_command = run.call_args_list[2].args[0]
        safe_dir_command = run.call_args_list[3].args[0]
        exec_command = run.call_args_list[4].args[0]
        self.assertEqual(output, "ok\nwarn")
        self.assertNotIn(f"{repo}:/work", start_command)
        self.assertNotIn(f"{repo.parent}:/changes:ro", start_command)
        self.assertEqual(start_command[start_command.index("-w") + 1], "/")
        self.assertEqual(
            mkdir_command,
            ["docker", "exec", "-w", "/", "container-1", "mkdir", "-p", "/workspace/repo"],
        )
        self.assertEqual(copy_command, ["docker", "cp", f"{repo}/.", "container-1:/workspace/repo"])
        self.assertEqual(
            safe_dir_command,
            [
                "docker",
                "exec",
                "-w",
                "/",
                "container-1",
                "sh",
                "-lc",
                "command -v git >/dev/null && "
                "git config --global --add safe.directory /workspace/repo || true",
            ],
        )
        self.assertEqual(
            exec_command,
            [
                "docker",
                "exec",
                "-w",
                "/workspace/repo",
                "-e",
                "PATH=/workspace/repo/.venv/bin:/workspace/repo/.autobot-bootstrap/bin:"
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "-e",
                "UV_PROJECT_ENVIRONMENT=/workspace/repo/.venv",
                "container-1",
                "sh",
                "-c",
                "pytest",
            ],
        )

    def test_docker_run_passes_env_names_without_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            started = SimpleNamespace(returncode=0, stdout="container-1\n", stderr="")
            completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

            with patch("autobot.sandbox.subprocess.run", side_effect=[started, completed]) as run:
                DockerSandbox(
                    repo,
                    "python:3.12-slim",
                    env_names=["OPENROUTER_API_KEY"],
                ).run("true")

        start_command = run.call_args_list[0].args[0]
        self.assertIn("-e", start_command)
        self.assertIn("OPENROUTER_API_KEY", start_command)
        self.assertNotIn("OPENROUTER_API_KEY=", start_command)

    def test_docker_apply_changes_mounts_payload_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            started = SimpleNamespace(returncode=0, stdout="container-1\n", stderr="")
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("autobot.sandbox.subprocess.run", side_effect=[started, completed]) as run:
                DockerSandbox(repo, "python:3.12-slim").apply_changes(
                    [FileChange("README.md", "# Demo\n")]
                )

            payload = json.loads((repo.parent / "changes.json").read_text(encoding="utf-8"))

        start_command = run.call_args_list[0].args[0]
        exec_command = run.call_args_list[1].args[0]
        self.assertEqual(payload[0]["path"], "README.md")
        self.assertIn(f"{repo.parent}:/changes:ro", start_command)
        self.assertEqual(start_command[start_command.index("--network") + 1], "none")
        self.assertEqual(exec_command[:4], ["docker", "exec", "-w", "/work"])
        self.assertTrue(
            any(
                item.startswith("PATH=/work/.venv/bin:/work/.autobot-bootstrap/bin:")
                for item in exec_command
            )
        )
        self.assertIn("UV_PROJECT_ENVIRONMENT=/work/.venv", exec_command)
        self.assertIn("container-1", exec_command)
        container_index = exec_command.index("container-1")
        self.assertEqual(exec_command[container_index + 1 : container_index + 3], ["python", "-c"])

    def test_docker_copy_apply_changes_uses_docker_cp_for_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            started = SimpleNamespace(returncode=0, stdout="container-1\n", stderr="")
            ok = SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch(
                "autobot.sandbox.subprocess.run", side_effect=[started, ok, ok, ok, ok, ok]
            ) as run:
                DockerSandbox(repo, "python:3.12-slim", mode="copy").apply_changes(
                    [FileChange("README.md", "# Demo\n")]
                )

            payload = json.loads((repo.parent / "changes.json").read_text(encoding="utf-8"))

        start_command = run.call_args_list[0].args[0]
        payload_copy = run.call_args_list[4].args[0]
        apply_command = run.call_args_list[5].args[0]
        self.assertEqual(payload[0]["path"], "README.md")
        self.assertNotIn(f"{repo.parent}:/changes:ro", start_command)
        self.assertEqual(
            payload_copy,
            [
                "docker",
                "cp",
                str(repo.parent / "changes.json"),
                "container-1:/tmp/autobot-changes.json",
            ],
        )
        self.assertEqual(apply_command[:4], ["docker", "exec", "-w", "/workspace/repo"])
        self.assertTrue(
            any(
                item.startswith(
                    "PATH=/workspace/repo/.venv/bin:/workspace/repo/.autobot-bootstrap/bin:"
                )
                for item in apply_command
            )
        )
        self.assertIn("UV_PROJECT_ENVIRONMENT=/workspace/repo/.venv", apply_command)
        self.assertIn("container-1", apply_command)
        container_index = apply_command.index("container-1")
        self.assertEqual(apply_command[container_index + 1 : container_index + 3], ["python", "-c"])

    def test_docker_copy_syncs_requested_file_to_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            target = repo / "src" / "demo.py"
            started = SimpleNamespace(returncode=0, stdout="container-1\n", stderr="")
            ok = SimpleNamespace(returncode=0, stdout="", stderr="")
            exists = SimpleNamespace(returncode=0, stdout="", stderr="")
            copied = SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch(
                "autobot.sandbox.subprocess.run",
                side_effect=[started, ok, ok, ok, exists, copied],
            ) as run:
                DockerSandbox(repo, "python:3.12-slim", mode="copy").sync_to_host(["src/demo.py"])

        self.assertFalse(target.exists())
        self.assertEqual(
            run.call_args_list[4].args[0],
            [
                "docker",
                "exec",
                "-w",
                "/workspace/repo",
                "container-1",
                "test",
                "-e",
                "src/demo.py",
            ],
        )
        self.assertEqual(
            run.call_args_list[5].args[0],
            [
                "docker",
                "cp",
                "container-1:/workspace/repo/src/demo.py",
                str(target.parent.resolve()),
            ],
        )

    def test_docker_copy_sync_removes_deleted_host_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            target = repo / "old.txt"
            repo.mkdir()
            target.write_text("old\n", encoding="utf-8")
            started = SimpleNamespace(returncode=0, stdout="container-1\n", stderr="")
            ok = SimpleNamespace(returncode=0, stdout="", stderr="")
            missing = SimpleNamespace(returncode=1, stdout="", stderr="")

            with patch(
                "autobot.sandbox.subprocess.run",
                side_effect=[started, ok, ok, ok, missing],
            ):
                DockerSandbox(repo, "python:3.12-slim", mode="copy").sync_to_host(["old.txt"])

        self.assertFalse(target.exists())

    def test_docker_prepare_and_run_share_one_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            started = SimpleNamespace(returncode=0, stdout="container-1\n", stderr="")
            setup = SimpleNamespace(returncode=0, stdout="setup\n", stderr="")
            verified = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
            sandbox = DockerSandbox(repo, "python:3.12-slim", "python -m pip install -e .")

            with patch(
                "autobot.sandbox.subprocess.run",
                side_effect=[started, setup, verified],
            ) as run:
                sandbox.prepare()
                output = sandbox.run("python -m pytest")

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(output, "ok")
        self.assertEqual(
            [command[:2] for command in commands],
            [["docker", "run"], ["docker", "exec"], ["docker", "exec"]],
        )
        self.assertIn("container-1", commands[1])
        self.assertIn("container-1", commands[2])
        self.assertIn("UV_PROJECT_ENVIRONMENT=/work/.venv", commands[1])
        self.assertIn("UV_PROJECT_ENVIRONMENT=/work/.venv", commands[2])

    def test_docker_apply_changes_rejects_secret_like_payload_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            token = "ghp_" + ("A" * 36)

            with (
                patch("autobot.sandbox.subprocess.run") as run,
                self.assertRaises(SandboxError) as raised,
            ):
                DockerSandbox(repo, "python:3.12-slim").apply_changes(
                    [FileChange("README.md", f"{token}\n")]
                )

            self.assertFalse(run.called)
            self.assertFalse((repo.parent / "changes.json").exists())
            self.assertNotIn(token, str(raised.exception))
            self.assertIn("secret-like values found in proposed changes", str(raised.exception))

    def test_docker_apply_changes_rejects_path_traversal_before_writing_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            with (
                patch("autobot.sandbox.subprocess.run") as run,
                self.assertRaises(SandboxError) as raised,
            ):
                DockerSandbox(repo, "python:3.12-slim").apply_changes(
                    [FileChange("../escape.txt", "no\n")]
                )

            self.assertFalse(run.called)
            self.assertFalse((repo.parent / "changes.json").exists())
            self.assertIn("change path escapes repository", str(raised.exception))

    def test_docker_run_redacts_failed_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            token = "ghp_" + ("A" * 36)
            started = SimpleNamespace(returncode=0, stdout="container-1\n", stderr="")
            failed = SimpleNamespace(returncode=1, stdout=f"bad {token}\n", stderr="")

            with (
                patch("autobot.sandbox.subprocess.run", side_effect=[started, failed]),
                self.assertRaises(SandboxError) as raised,
            ):
                DockerSandbox(repo, "python:3.12-slim").run("pytest")

        self.assertNotIn(token, str(raised.exception))
        self.assertIn("[redacted-secret]", str(raised.exception))

    def test_docker_prepare_rejects_secret_like_setup_command_before_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            token = "sk-" + ("A" * 40)

            with (
                patch("autobot.sandbox.subprocess.run") as run,
                self.assertRaises(SandboxError) as raised,
            ):
                DockerSandbox(repo, "python:3.12-slim", f"echo {token}").prepare()

            self.assertFalse(run.called)
            self.assertNotIn(token, str(raised.exception))
            self.assertIn(
                "secret-like values found in sandbox setup command", str(raised.exception)
            )

    def test_local_sandbox_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()

            with self.assertRaises(SandboxError):
                LocalSandbox(root).apply_changes([FileChange("../escape.txt", "no\n")])

            self.assertFalse((root.parent / "escape.txt").exists())

    def test_local_sandbox_deletes_directories_like_docker_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            docs = root / "docs"
            docs.mkdir(parents=True)
            (docs / "old.md").write_text("old\n", encoding="utf-8")

            LocalSandbox(root).apply_changes([FileChange("docs", None, "delete")])

            self.assertFalse(docs.exists())

    def test_local_sandbox_rejects_secret_like_payload_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            token = "sk-" + ("A" * 40)

            with self.assertRaises(SandboxError) as raised:
                LocalSandbox(root).apply_changes([FileChange("README.md", f"{token}\n")])

            self.assertFalse((root / "README.md").exists())
            self.assertNotIn(token, str(raised.exception))
            self.assertIn("secret-like values found in proposed changes", str(raised.exception))

    def test_local_sandbox_run_redacts_failed_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            token = "ghp_" + ("A" * 36)
            failed = SimpleNamespace(returncode=1, stdout="", stderr=f"bad {token}\n")

            with (
                patch("autobot.sandbox.subprocess.run", return_value=failed),
                self.assertRaises(SandboxError) as raised,
            ):
                LocalSandbox(root).run("pytest")

        self.assertNotIn(token, str(raised.exception))
        self.assertIn("[redacted-secret]", str(raised.exception))

    def test_local_sandbox_run_prefers_prepared_repo_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

            with patch("autobot.sandbox.subprocess.run", return_value=completed) as run:
                output = LocalSandbox(root).run("python -m pytest")

        command = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertEqual(output, "ok")
        self.assertEqual(command, ["sh", "-c", "python -m pytest"])
        self.assertTrue(env["PATH"].startswith(f"{root / '.venv' / 'bin'}:"))
        self.assertIn(f"{root / '.autobot-bootstrap' / 'bin'}:", env["PATH"])
        self.assertEqual(env["UV_PROJECT_ENVIRONMENT"], str(root / ".venv"))


if __name__ == "__main__":
    unittest.main()
