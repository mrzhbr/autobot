from __future__ import annotations

DEFAULT_BRANCHES = {"main", "master", "trunk", "develop"}


def is_default_like_branch(branch: str) -> bool:
    candidates = [branch]
    for separator in (":", "/", "refs/heads/"):
        if separator in branch:
            candidates.append(branch.rsplit(separator, 1)[-1])
    return any(candidate in DEFAULT_BRANCHES for candidate in candidates)
