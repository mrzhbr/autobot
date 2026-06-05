from __future__ import annotations

import configparser
import json
import shutil
import subprocess
import tomllib
from dataclasses import asdict
from pathlib import Path

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
    def __init__(
        self,
        repo_dir: Path,
        image: str,
        setup_command: str | None = None,
        network: str = "none",
    ) -> None:
        self.repo_dir = repo_dir
        self.image = image
        self.setup_command = setup_command
        self.network = network
        self.container_id: str | None = None

    def prepare(self) -> None:
        if self.setup_command:
            ensure_no_secret_commands([self.setup_command], "sandbox setup command")
        self._ensure_started()
        if self.setup_command:
            self.run(self.setup_command, timeout=1800)

    def apply_changes(self, changes: list[FileChange]) -> None:
        _ensure_no_secret_changes(changes)
        payload = [asdict(change) for change in changes]
        change_file = self.repo_dir.parent / "changes.json"
        change_file.write_text(json.dumps(payload), encoding="utf-8")
        script = (
            "import json, pathlib, shutil\n"
            "root = pathlib.Path('/work').resolve()\n"
            "changes = json.load(open('/changes/changes.json', encoding='utf-8'))\n"
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
        return self._exec(["sh", "-lc", command], timeout=timeout)

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

    def _ensure_started(self) -> None:
        if self.container_id:
            return
        cmd = [
            "docker",
            "run",
            "-d",
            "--rm",
            "--network",
            self.network,
            "-v",
            f"{self.repo_dir}:/work",
            "-v",
            f"{self.repo_dir.parent}:/changes:ro",
            "-w",
            "/work",
            self.image,
            "sh",
            "-lc",
            "while true; do sleep 3600; done",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        self.container_id = _checked_output(result) or None
        if not self.container_id:
            raise SandboxError("docker did not return a container id")

    def _exec(self, args: list[str], timeout: int = 900) -> str:
        self._ensure_started()
        cmd = ["docker", "exec", "-w", "/work", self.container_id or "", *args]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return _checked_output(result)


def _checked_output(result: subprocess.CompletedProcess) -> str:
    if result.returncode != 0:
        output = redact_secret_like_values((result.stdout + "\n" + result.stderr).strip())
        raise SandboxError(output or "sandbox command failed")
    return result.stdout.strip()


class LocalSandbox:
    """Test-only sandbox that applies changes on the host."""

    def __init__(self, repo_dir: Path) -> None:
        self.repo_dir = repo_dir

    def prepare(self) -> None:
        return None

    def apply_changes(self, changes: list[FileChange]) -> None:
        _ensure_no_secret_changes(changes)
        for change in changes:
            target = (self.repo_dir / change.path).resolve()
            target.relative_to(self.repo_dir.resolve())
            if change.action == "delete":
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.content or "", encoding="utf-8")

    def run(self, command: str, timeout: int = 900) -> str:
        result = subprocess.run(
            ["sh", "-lc", command],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            output = redact_secret_like_values((result.stdout + "\n" + result.stderr).strip())
            raise SandboxError(output or "sandbox command failed")
        return result.stdout.strip()


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
    ensure_no_secret_commands(commands)
    output: list[str] = []
    for command in commands:
        if dry_run:
            output.append(_verification_block(command, "dry-run skipped"))
        else:
            output.append(_verification_block(command, sandbox.run(command)))
    return "\n\n".join(output)


def run_verification_allow_failure(
    sandbox: DockerSandbox,
    commands: list[str],
    dry_run: bool,
) -> dict:
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


def _verification_block(command: str, text: str) -> str:
    return redact_secret_like_values(f"$ {command}\n{text}")


def _ensure_no_secret_changes(changes: list[FileChange]) -> None:
    text = "\n".join(f"{change.path}\n{change.content or ''}" for change in changes)
    _sandbox_secret_check(text, "proposed changes")


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
    commands = []
    if (repo_dir / "requirements-dev.txt").exists():
        commands.append("python -m pip install -r requirements-dev.txt")
    elif (repo_dir / "requirements.txt").exists():
        commands.append("python -m pip install -r requirements.txt")
    if any((repo_dir / name).exists() for name in ("pyproject.toml", "setup.py", "setup.cfg")):
        if _has_dev_extra(repo_dir):
            commands.append('python -m pip install -e ".[dev]"')
        else:
            commands.append("python -m pip install -e .")
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
