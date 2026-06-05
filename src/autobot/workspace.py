from __future__ import annotations

import subprocess
from pathlib import Path

from autobot.models import Issue


def repo_work_dir(work_root: Path, issue: Issue) -> Path:
    repo_key = issue.repo.replace("/", "__")
    return work_root / repo_key / str(issue.number) / "repo"


def branch_name(issue: Issue) -> str:
    slug = "".join(char if char.isalnum() else "-" for char in issue.title.lower()).strip("-")
    slug = "-".join(filter(None, slug.split("-")))[:48] or "change"
    return f"autobot/issue-{issue.number}-{slug}"


def prepare_dry_run_repo(repo_dir: Path, branch: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / ".git").exists():
        _run(["git", "init"], repo_dir)
        _run(["git", "config", "user.email", "autobot@example.invalid"], repo_dir)
        _run(["git", "config", "user.name", "Autobot"], repo_dir)
        (repo_dir / "README.md").write_text("# Dry run repo\n", encoding="utf-8")
        _run(["git", "add", "README.md"], repo_dir)
        _run(["git", "commit", "-m", "chore: initial dry run"], repo_dir)
    _run(["git", "checkout", "-B", branch], repo_dir)


def changed_files(repo_dir: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _run(args: list[str], repo_dir: Path) -> None:
    subprocess.run(args, cwd=repo_dir, check=True, capture_output=True, text=True)
