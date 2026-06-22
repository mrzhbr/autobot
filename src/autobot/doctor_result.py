from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from autobot.scanner import redact_secret_like_values


class CommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: int,
    ) -> Any:
        """Run a command and return an object with returncode/stdout/stderr."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", redact_secret_like_values(self.message))

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "message": self.message}
