from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autobot.models import utc_now


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, action: str, repo: str, issue_number: int, details: dict[str, Any]) -> None:
        row = {
            "at": utc_now(),
            "action": action,
            "repo": repo,
            "issue_number": issue_number,
            "details": self._sanitize(details),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: self._sanitize(val) for key, val in value.items() if "token" not in key.lower()
            }
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        if isinstance(value, str) and len(value) > 1200:
            return value[:1200] + "...[truncated]"
        return value
