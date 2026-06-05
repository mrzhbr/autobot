from __future__ import annotations

import re
from pathlib import Path

from autobot.models import ContextFile, Issue

IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".autobot",
}
TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".go",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".rb",
    ".rs",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}


def gather_context(
    repo_dir: Path,
    issue: Issue,
    max_files: int = 12,
    max_bytes: int = 5000,
) -> list[ContextFile]:
    candidates = _priority_files(repo_dir)
    candidates.extend(_keyword_matches(repo_dir, _keywords(issue)))
    seen: set[Path] = set()
    files: list[ContextFile] = []
    for path in candidates:
        if path in seen or not _is_text_file(path):
            continue
        seen.add(path)
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(repo_dir).as_posix()
        files.append(ContextFile(path=rel, content=content[:max_bytes]))
        if len(files) >= max_files:
            break
    return files


def format_context(files: list[ContextFile]) -> str:
    chunks = []
    for item in files:
        chunks.append(f"### {item.path}\n```text\n{item.content}\n```")
    return "\n\n".join(chunks)


def _priority_files(repo_dir: Path) -> list[Path]:
    names = [
        "README.md",
        "pyproject.toml",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "requirements.txt",
    ]
    return [repo_dir / name for name in names if (repo_dir / name).exists()]


def _keywords(issue: Issue) -> list[str]:
    text = f"{issue.title} {issue.body}".lower()
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text)
    stop = {"the", "and", "for", "with", "from", "this", "that", "add", "fix", "issue"}
    return [word for word in dict.fromkeys(words) if word not in stop][:8]


def _keyword_matches(repo_dir: Path, keywords: list[str]) -> list[Path]:
    if not keywords:
        return []
    scored: list[tuple[int, Path]] = []
    for path in repo_dir.rglob("*"):
        if not path.is_file() or _ignored(path, repo_dir) or not _is_text_file(path):
            continue
        rel = path.relative_to(repo_dir).as_posix().lower()
        score = sum(2 for word in keywords if word in rel)
        if score == 0 and path.stat().st_size <= 80_000:
            try:
                sample = path.read_text(encoding="utf-8", errors="ignore")[:20_000].lower()
            except OSError:
                continue
            score = sum(1 for word in keywords if word in sample)
        if score:
            scored.append((score, path))
    scored.sort(key=lambda item: (-item[0], item[1].as_posix()))
    return [path for _, path in scored]


def _ignored(path: Path, repo_dir: Path) -> bool:
    rel_parts = path.relative_to(repo_dir).parts
    return any(part in IGNORE_DIRS for part in rel_parts)


def _is_text_file(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name in {"Makefile", "Dockerfile"}
