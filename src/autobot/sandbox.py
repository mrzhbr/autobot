from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from autobot.models import FileChange
from autobot.scanner import ensure_no_secret_like_values, redact_secret_like_values


class SandboxError(RuntimeError):
    pass


def detect_setup_command(repo_dir: Path, configured: str | None) -> str | None:
    if configured:
        return configured
    if _has_python_setup(repo_dir):
        return _python_setup(repo_dir)
    if (repo_dir / "package.json").exists():
        return _node_setup(repo_dir)
    if (repo_dir / "go.mod").exists():
        return "go mod download"
    if (repo_dir / "Cargo.toml").exists():
        return "cargo fetch"
    return None


class DockerSandbox:
    WORKDIR = "/workspace/repo"

    def __init__(
        self,
        repo_dir: Path,
        image: str,
        setup_command: str | None = None,
        network: str = "none",
        env_names: list[str] | None = None,
        mode: Literal["bind", "copy"] = "bind",
    ) -> None:
        self.repo_dir = repo_dir
        self.image = image
        self.setup_command = setup_command
        self.network = network
        self.env_names = env_names or []
        self.mode = mode
        self.container_id: str | None = None
        self._workspace_ready = False

    def prepare(self) -> None:
        if self.setup_command:
            ensure_no_secret_commands([self.setup_command], "sandbox setup command")
        self._ensure_workspace()
        if self.setup_command:
            self.run(self.setup_command, timeout=1800)

    def apply_changes(self, changes: list[FileChange]) -> None:
        _ensure_relative_change_paths(self.repo_dir, changes)
        _ensure_no_secret_changes(changes)
        self._ensure_workspace()
        payload = [asdict(change) for change in changes]
        change_file = self.repo_dir.parent / "changes.json"
        change_file.write_text(json.dumps(payload), encoding="utf-8")
        payload_path = "/changes/changes.json"
        if self.mode == "copy":
            self._copy_file_to_container(change_file, "/tmp/autobot-changes.json")
            payload_path = "/tmp/autobot-changes.json"
        script = (
            "import json, pathlib, shutil\n"
            f"root = pathlib.Path('{self._workdir()}').resolve()\n"
            f"changes = json.load(open('{payload_path}', encoding='utf-8'))\n"
            "for change in changes:\n"
            "    target = (root / change['path']).resolve()\n"
            "    target.relative_to(root)\n"
            "    action = change.get('action') or 'write'\n"
            "    if action == 'delete':\n"
            "        if target.is_dir(): shutil.rmtree(target)\n"
            "        elif target.exists(): target.unlink()\n"
            "        continue\n"
            "    target.parent.mkdir(parents=True, exist_ok=True)\n"
            "    target.write_text(change.get('content') or '', encoding='utf-8')\n"
        )
        self._exec(["python", "-c", script])

    def run(self, command: str, timeout: int = 900) -> str:
        return self._exec(["sh", "-c", command], timeout=timeout)

    def sync_to_host(self, paths: list[str] | None = None) -> None:
        if self.mode == "bind":
            return
        self._ensure_workspace()
        resolved = _unique_paths(paths or self.changed_paths())
        _ensure_relative_paths(resolved)
        for path in resolved:
            self._sync_path_to_host(path)

    def changed_paths(self) -> list[str]:
        output = self._exec(["git", "status", "--porcelain", "--untracked-files=all"])
        paths: list[str] = []
        for line in output.splitlines():
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if path:
                paths.append(path)
        return _unique_paths(paths)

    def close(self) -> None:
        if not self.container_id:
            return
        subprocess.run(
            ["docker", "rm", "-f", self.container_id],
            capture_output=True,
            text=True,
            check=False,
        )
        self.container_id = None
        self._workspace_ready = False

    def _ensure_started(self) -> None:
        if self.container_id:
            return
        cmd = self._start_command()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        self.container_id = _checked_output(result) or None
        if not self.container_id:
            raise SandboxError("docker did not return a container id")

    def _ensure_workspace(self) -> None:
        self._ensure_started()
        if self.mode == "bind" or self._workspace_ready:
            return
        self._copy_repo_to_container()
        self._workspace_ready = True

    def _start_command(self) -> list[str]:
        cmd = [
            "docker",
            "run",
            "-d",
            "--rm",
            "--network",
            self.network,
            *_docker_env_args(self.env_names),
            "-w",
            "/work" if self.mode == "bind" else "/",
            self.image,
            "sh",
            "-c",
            "while true; do sleep 3600; done",
        ]
        if self.mode == "bind":
            insert_at = cmd.index("-w")
            cmd[insert_at:insert_at] = [
                "-v",
                f"{self.repo_dir}:/work",
                "-v",
                f"{self.repo_dir.parent}:/changes:ro",
            ]
        return cmd

    def popen(
        self,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen:
        self._ensure_workspace()
        process_env = self._repo_env(env)
        cmd = [
            "docker",
            "exec",
            "-i",
            "-w",
            self._workdir(),
            *_docker_env_value_args(process_env),
            self.container_id or "",
            *args,
        ]
        return subprocess.Popen(
            cmd,
            cwd=self.repo_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _exec(self, args: list[str], timeout: int = 900) -> str:
        self._ensure_workspace()
        cmd = [
            "docker",
            "exec",
            "-w",
            self._workdir(),
            *_docker_env_value_args(self._repo_env()),
            self.container_id or "",
            *args,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return _checked_output(result, include_stderr_on_success=True)

    def _workdir(self) -> str:
        return "/work" if self.mode == "bind" else self.WORKDIR

    def _repo_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {
            "PATH": (
                f"{self._workdir()}/.venv/bin:"
                f"{self._workdir()}/.autobot-bootstrap/bin:"
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            ),
            "UV_PROJECT_ENVIRONMENT": f"{self._workdir()}/.venv",
            **(extra or {}),
        }

    def _copy_repo_to_container(self) -> None:
        self._exec_at("/", ["mkdir", "-p", self.WORKDIR])
        result = subprocess.run(
            ["docker", "cp", f"{self.repo_dir}/.", f"{self.container_id}:{self.WORKDIR}"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        _checked_output(result)
        self._exec_at(
            "/",
            [
                "sh",
                "-lc",
                f"command -v git >/dev/null && "
                f"git config --global --add safe.directory {self.WORKDIR} || true",
            ],
        )

    def _copy_file_to_container(self, source: Path, target: str) -> None:
        self._ensure_started()
        result = subprocess.run(
            ["docker", "cp", str(source), f"{self.container_id}:{target}"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        _checked_output(result)

    def _sync_path_to_host(self, path: str) -> None:
        target = (self.repo_dir / path).resolve()
        target.relative_to(self.repo_dir.resolve())
        exists = self._exists_in_container(path)
        if not exists:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            return
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["docker", "cp", f"{self.container_id}:{self.WORKDIR}/{path}", str(target.parent)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        _checked_output(result)

    def _exists_in_container(self, path: str) -> bool:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-w",
                self.WORKDIR,
                self.container_id or "",
                "test",
                "-e",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        _checked_output(result)
        return False

    def _exec_at(self, workdir: str, args: list[str], timeout: int = 900) -> str:
        cmd = ["docker", "exec", "-w", workdir, self.container_id or "", *args]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return _checked_output(result)


def _checked_output(
    result: subprocess.CompletedProcess,
    include_stderr_on_success: bool = False,
) -> str:
    output = (
        _combined_output(result) if include_stderr_on_success else (result.stdout or "").strip()
    )
    if result.returncode != 0:
        raise SandboxError(
            redact_secret_like_values(_combined_output(result)) or "sandbox command failed"
        )
    return redact_secret_like_values(output)


def _combined_output(result: subprocess.CompletedProcess) -> str:
    return "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())


def _docker_env_args(names: list[str]) -> list[str]:
    args: list[str] = []
    for name in names:
        args.extend(["-e", name])
    return args


def _docker_env_value_args(values: dict[str, str]) -> list[str]:
    args: list[str] = []
    for name, value in values.items():
        args.extend(["-e", f"{name}={value}"])
    return args


def _unique_paths(paths: list[str]) -> list[str]:
    return list(dict.fromkeys(path for path in paths if path))


def _ensure_relative_paths(paths: list[str]) -> None:
    changes = [FileChange(path, "", "write") for path in paths]
    _ensure_relative_change_paths(Path("."), changes)


class LocalSandbox:
    """Test-only sandbox that applies changes on the host."""

    def __init__(self, repo_dir: Path) -> None:
        self.repo_dir = repo_dir

    def prepare(self) -> None:
        return None

    def apply_changes(self, changes: list[FileChange]) -> None:
        _ensure_relative_change_paths(self.repo_dir, changes)
        _ensure_no_secret_changes(changes)
        for change in changes:
            target = (self.repo_dir / change.path).resolve()
            if change.action == "delete":
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.content or "", encoding="utf-8")

    def run(self, command: str, timeout: int = 900) -> str:
        env = os.environ.copy()
        env["PATH"] = (
            f"{self.repo_dir / '.venv' / 'bin'}:"
            f"{self.repo_dir / '.autobot-bootstrap' / 'bin'}:"
            f"{env.get('PATH', '')}"
        )
        env["UV_PROJECT_ENVIRONMENT"] = str(self.repo_dir / ".venv")
        result = subprocess.run(
            ["sh", "-c", command],
            cwd=self.repo_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            output = redact_secret_like_values((result.stdout + "\n" + result.stderr).strip())
            raise SandboxError(output or "sandbox command failed")
        return _checked_output(result, include_stderr_on_success=True)


def apply_changes(
    repo_dir: Path,
    sandbox: DockerSandbox,
    changes: list[FileChange],
    dry_run: bool,
) -> None:
    if dry_run:
        LocalSandbox(repo_dir).apply_changes(changes)
    else:
        sandbox.apply_changes(changes)


def run_verification(sandbox: DockerSandbox, commands: list[str], dry_run: bool) -> str:
    commands = normalize_verification_commands(commands)
    ensure_no_secret_commands(commands)
    output: list[str] = []
    for command in commands:
        if dry_run:
            output.append(_verification_block(command, "dry-run skipped"))
        else:
            try:
                output.append(_verification_block(command, sandbox.run(command)))
            except SandboxError as exc:
                raise SandboxError(_verification_block(command, str(exc))) from exc
    return "\n\n".join(output)


def run_verification_allow_failure(
    sandbox: DockerSandbox,
    commands: list[str],
    dry_run: bool,
) -> dict:
    commands = normalize_verification_commands(commands)
    ensure_no_secret_commands(commands)
    output: list[str] = []
    ok = True
    for command in commands:
        if dry_run:
            output.append(_verification_block(command, "dry-run skipped"))
            continue
        try:
            output.append(_verification_block(command, sandbox.run(command)))
        except SandboxError as exc:
            ok = False
            output.append(_verification_block(command, str(exc)))
    return {"ok": ok, "output": "\n\n".join(output)}


def normalize_verification_commands(commands: list[str]) -> list[str]:
    return [_normalize_verification_command(command) for command in commands]


def _normalize_verification_command(command: str) -> str:
    if "\\n" not in command or "python" not in command or " -c " not in command:
        return command
    return command.replace("\\n", "\n")


def _verification_block(command: str, text: str) -> str:
    return redact_secret_like_values(f"$ {command}\n{text}")


def _ensure_no_secret_changes(changes: list[FileChange]) -> None:
    text = "\n".join(f"{change.path}\n{change.content or ''}" for change in changes)
    _sandbox_secret_check(text, "proposed changes")


def _ensure_relative_change_paths(repo_dir: Path, changes: list[FileChange]) -> None:
    root = repo_dir.resolve()
    for change in changes:
        try:
            (root / change.path).resolve().relative_to(root)
        except ValueError as exc:
            raise SandboxError("change path escapes repository") from exc


def ensure_no_secret_commands(commands: list[str], surface: str = "verification commands") -> None:
    _sandbox_secret_check("\n".join(commands), surface)


def _sandbox_secret_check(text: str, surface: str) -> None:
    try:
        ensure_no_secret_like_values(text, surface)
    except RuntimeError as exc:
        raise SandboxError(str(exc)) from exc


def _has_python_setup(repo_dir: Path) -> bool:
    return any(
        (repo_dir / name).exists()
        for name in (
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "requirements-dev.txt",
            "requirements.txt",
        )
    )


def _python_setup(repo_dir: Path) -> str:
    if (repo_dir / "pyproject.toml").exists():
        lock_mode = " --frozen" if (repo_dir / "uv.lock").exists() else ""
        python = _python_version_option(repo_dir)
        return (
            "python -m venv .autobot-bootstrap"
            " && .autobot-bootstrap/bin/python -m pip install --upgrade pip uv"
            " && UV_PROJECT_ENVIRONMENT=.venv UV_PYTHON_PREFERENCE=managed "
            f".autobot-bootstrap/bin/uv sync{python}{lock_mode} --all-extras --dev"
        )
    commands = []
    if (repo_dir / "requirements-dev.txt").exists():
        commands.extend(
            [
                "python -m venv .venv",
                ".venv/bin/python -m pip install --upgrade pip",
                ".venv/bin/python -m pip install -r requirements-dev.txt",
            ]
        )
    elif (repo_dir / "requirements.txt").exists():
        commands.extend(
            [
                "python -m venv .venv",
                ".venv/bin/python -m pip install --upgrade pip",
                ".venv/bin/python -m pip install -r requirements.txt",
            ]
        )
    if any((repo_dir / name).exists() for name in ("setup.py", "setup.cfg")):
        if not commands:
            commands.extend(
                ["python -m venv .venv", ".venv/bin/python -m pip install --upgrade pip"]
            )
        if _has_dev_extra(repo_dir):
            commands.append('.venv/bin/python -m pip install -e ".[dev]"')
        else:
            commands.append(".venv/bin/python -m pip install -e .")
    return " && ".join(commands)


def _has_dev_extra(repo_dir: Path) -> bool:
    return _pyproject_has_dev_extra(repo_dir / "pyproject.toml") or _setup_cfg_has_dev_extra(
        repo_dir / "setup.cfg"
    )


def _pyproject_has_dev_extra(path: Path) -> bool:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    extras = data.get("project", {}).get("optional-dependencies", {})
    return isinstance(extras, dict) and "dev" in extras


def _python_version_option(repo_dir: Path) -> str:
    version = _python_version_from_pyproject(repo_dir / "pyproject.toml")
    return f" --python {version}" if version else ""


def _python_version_from_pyproject(path: Path) -> str | None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    requires_python = data.get("project", {}).get("requires-python")
    if isinstance(requires_python, str):
        version = _lower_bound_python_version(requires_python)
        if version:
            return version
    ruff_target = data.get("tool", {}).get("ruff", {}).get("target-version")
    if isinstance(ruff_target, str):
        version = _py_tag_version(ruff_target)
        if version:
            return version
    mypy_version = data.get("tool", {}).get("mypy", {}).get("python_version")
    if isinstance(mypy_version, str) and re.fullmatch(r"\d+\.\d+", mypy_version):
        return mypy_version
    return None


def _lower_bound_python_version(specifier: str) -> str | None:
    matches = re.findall(r"(?:>=|==|~=)\s*(\d+)\.(\d+)", specifier)
    if not matches:
        return None
    major, minor = min((int(major), int(minor)) for major, minor in matches)
    return f"{major}.{minor}"


def _py_tag_version(value: str) -> str | None:
    match = re.fullmatch(r"py(\d)(\d{2})", value)
    if not match:
        return None
    return f"{int(match.group(1))}.{int(match.group(2))}"


def _setup_cfg_has_dev_extra(path: Path) -> bool:
    parser = configparser.ConfigParser()
    if not path.exists() or not parser.read(path):
        return False
    return parser.has_option("options.extras_require", "dev")


def _node_setup(repo_dir: Path) -> str:
    if (repo_dir / "pnpm-lock.yaml").exists():
        return "corepack enable && pnpm install --frozen-lockfile"
    if (repo_dir / "yarn.lock").exists():
        return "corepack enable && yarn install --frozen-lockfile"
    if (repo_dir / "package-lock.json").exists() or (repo_dir / "npm-shrinkwrap.json").exists():
        return "npm ci"
    return "npm install"
