from __future__ import annotations

import re

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b"),
    re.compile(
        r"(?i)(api[_-]?key|token|secret|password|credential|authorization)"
        r"\s*[:=]\s*['\"][A-Za-z0-9_./+=-]{24,}['\"]"
    ),
]


def find_secret_like_values(text: str) -> list[str]:
    findings: list[str] = []
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(match.group(0)[:120])
    return findings


def ensure_no_secret_like_values(text: str, surface: str) -> None:
    if secrets := find_secret_like_values(text):
        count = len(secrets)
        raise RuntimeError(f"secret-like values found in {surface}: {count} finding(s)")


def redact_secret_like_values(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[redacted-secret]", redacted)
    return redacted
