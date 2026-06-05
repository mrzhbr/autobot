from __future__ import annotations

import re

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*['\"][A-Za-z0-9_./+=-]{24,}['\"]"),
]


def find_secret_like_values(text: str) -> list[str]:
    findings: list[str] = []
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(match.group(0)[:120])
    return findings
