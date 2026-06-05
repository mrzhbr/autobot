from __future__ import annotations

import json
import shutil
import subprocess
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

    def prepare(self) -> None:
        if self.setup_command:
            ensure_no_secret_commands([self.setup_command], "sandbox setup command")
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
        self._docker(["python", "-c", script], mounts=[(change_file.parent, "/changes:ro")])

    def run(self, command: str, timeout: int = 900) -> str:
        return self._docker(["sh", "-lc", command], timeout=timeout)

    def _docker(
        self,
        args: list[str],
        timeout: int = 900,
        mounts: list[tuple[Path, str]] | None = None,
    ) -> str:
        cmd = [
            "docker",
            "run",
            "--rm",
            "--network",
            self.network,
            "-v",
            f"{self.repo_dir}:/work",
            "-w",
            "/work",
        ]
        for host, container in mounts or []:
            cmd.extend(["-v", f"{host}:{container}"])
        cmd.extend([self.image, *args])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode != 0:
            output = (result.stdout + "\n" + result.stderr).strip()
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
            raise SandboxError((result.stdout + "\n" + result.stderr).strip())
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
        commands.append('python -m pip install -e ".[dev]"')
    return " && ".join(commands)


def _node_setup(repo_dir: Path) -> str:
    if (repo_dir / "pnpm-lock.yaml").exists():
        return "corepack enable && pnpm install --frozen-lockfile"
    if (repo_dir / "yarn.lock").exists():
        return "corepack enable && yarn install --frozen-lockfile"
    if (repo_dir / "package-lock.json").exists() or (repo_dir / "npm-shrinkwrap.json").exists():
        return "npm ci"
    return "npm install"
