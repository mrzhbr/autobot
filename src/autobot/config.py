from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    root: Path
    db_path: Path
    audit_path: Path
    work_root: Path
    github_token: str | None
    agent_login: str | None
    llm_provider: str | None
    triage_model: str
    implement_model: str
    review_model: str
    review_models: list[str]
    sandbox_image: str
    sandbox_network: str
    sandbox_setup_command: str | None
    default_test_command: str | None
    max_review_rounds: int
    max_issue_tokens: int | None
    max_issue_dollars: float | None
    comment_limit: int
    dry_run: bool = False
    mock_llm: bool = False

    @classmethod
    def from_env(
        cls,
        root: Path,
        db_path: str | None = None,
        work_root: str | None = None,
        dry_run: bool = False,
        mock_llm: bool = False,
    ) -> Config:
        base = root / ".autobot"
        model = os.getenv("MODEL", "gpt-4.1")
        db_default = os.getenv("AUTOBOT_DB", str(base / "state.db"))
        audit_default = os.getenv("AUTOBOT_AUDIT_LOG", str(base / "audit.jsonl"))
        work_default = os.getenv("AUTOBOT_WORK_ROOT", str(base / "work"))
        return cls(
            root=root,
            db_path=Path(db_path) if db_path else Path(db_default),
            audit_path=Path(audit_default),
            work_root=Path(work_root) if work_root else Path(work_default),
            github_token=os.getenv("GITHUB_TOKEN"),
            agent_login=os.getenv("AGENT_LOGIN") or os.getenv("GITHUB_ACTOR"),
            llm_provider=os.getenv("LLM_PROVIDER"),
            triage_model=os.getenv("TRIAGE_MODEL", model),
            implement_model=os.getenv("IMPLEMENT_MODEL", model),
            review_model=os.getenv("REVIEW_MODEL", model),
            review_models=_model_list(os.getenv("REVIEW_MODELS"), os.getenv("REVIEW_MODEL", model)),
            sandbox_image=os.getenv("SANDBOX_IMAGE", "python:3.12-slim"),
            sandbox_network=os.getenv("SANDBOX_NETWORK", "none"),
            sandbox_setup_command=os.getenv("SANDBOX_SETUP_COMMAND"),
            default_test_command=os.getenv("AUTO_TEST_COMMAND"),
            max_review_rounds=int(os.getenv("MAX_REVIEW_ROUNDS", "3")),
            max_issue_tokens=_optional_int(os.getenv("MAX_ISSUE_TOKENS")),
            max_issue_dollars=_optional_float(os.getenv("MAX_ISSUE_DOLLARS")),
            comment_limit=int(os.getenv("COMMENT_LIMIT_PER_RUN", "2")),
            dry_run=dry_run,
            mock_llm=mock_llm or os.getenv("AUTOBOT_MOCK_LLM") == "1",
        )


def _optional_int(value: str | None) -> int | None:
    return int(value) if value else None


def _optional_float(value: str | None) -> float | None:
    return float(value) if value else None


def _model_list(value: str | None, fallback: str) -> list[str]:
    if not value:
        return [fallback]
    models = [item.strip() for item in value.split(",") if item.strip()]
    return models or [fallback]
