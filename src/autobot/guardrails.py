from __future__ import annotations

import re

from autobot.models import Issue
from autobot.scanner import find_secret_like_values

PATTERNS = {
    "authentication": re.compile(r"\b(auth|login|logout|oauth|sso|permission|rbac)\b", re.I),
    "cryptography": re.compile(r"\b(crypto|cryptography|encrypt|decrypt|cipher|hashing?)\b", re.I),
    "secrets handling": re.compile(r"\b(secret|token|api key|credential|password)\b", re.I),
    "database migration": re.compile(r"\b(migration|migrate|schema change|alter table)\b", re.I),
}


def detect_out_of_scope(issue: Issue, replies: list[dict] | None = None) -> list[str]:
    scope_text = _scope_text(issue, replies)
    topics = [name for name, pattern in PATTERNS.items() if pattern.search(scope_text)]
    if find_secret_like_values(_issue_text(issue)) and "secrets handling" not in topics:
        topics.append("secrets handling")
    return topics


def _scope_text(issue: Issue, replies: list[dict] | None) -> str:
    parts = [issue.title, issue.body]
    parts.extend(str(reply.get("body") or "") for reply in replies or [])
    return "\n".join(part for part in parts if part)


def _issue_text(issue: Issue) -> str:
    parts = [issue.title, issue.body]
    parts.extend(comment.body for comment in issue.comments)
    return "\n".join(part for part in parts if part)


def guardrail_question(topics: list[str]) -> str:
    joined = ", ".join(topics)
    return (
        f"This appears to require {joined}, which is outside the autonomous-change scope. "
        "Please narrow the issue to a safe non-sensitive change or have a human own it."
    )
