from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from autobot.models import FileChange


class SandboxError(RuntimeError):
    pass


class DockerSandbox:
    def __init__(
        self,
        repo_dir: Path,
        image: str,
        setup_command: str | None = None,
        network: str = "bridge",
    ) -> None:
        self.repo_dir = repo_dir
        self.image = image
        self.setup_command = setup_command
        self.network = network

    def prepare(self) -> None:
        if self.setup_command:
            self.run(self.setup_command, timeout=1800)

    def apply_changes(self, changes: list[FileChange]) -> None:
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
        for change in changes:
            target = (self.repo_dir / change.path).resolve()
            target.relative_to(self.repo_dir.resolve())
            if change.action == "delete":
                if target.exists():
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
