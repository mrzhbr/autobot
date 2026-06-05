from __future__ import annotations

import difflib
from pathlib import Path


def render_untracked_diff(repo_dir: Path, paths: list[str]) -> str:
    hunks: list[str] = []
    root = repo_dir.resolve()
    for path in paths:
        target = (repo_dir / path).resolve()
        target.relative_to(root)
        if not target.is_file():
            continue
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        hunk = difflib.unified_diff(
            [], lines, fromfile="/dev/null", tofile=f"b/{path}", lineterm=""
        )
        hunks.append(f"diff --git a/{path} b/{path}\nnew file mode 100644\n" + "\n".join(hunk))
    return "\n".join(hunks)
