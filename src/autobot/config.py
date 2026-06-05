from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

OPENAI_DEFAULT_MODEL = "gpt-4.1"
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-20250514"
OPENAI_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4")
ANTHROPIC_MODEL_PREFIXES = ("claude-",)
LLM_KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


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
        provider = os.getenv("LLM_PROVIDER")
        model = os.getenv("MODEL") or _default_model(infer_llm_provider(provider))
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
            llm_provider=provider,
            triage_model=os.getenv("TRIAGE_MODEL", model),
            implement_model=os.getenv("IMPLEMENT_MODEL", model),
            review_model=os.getenv("REVIEW_MODEL", model),
            review_models=_model_list(os.getenv("REVIEW_MODELS"), os.getenv("REVIEW_MODEL", model)),
            sandbox_image=os.getenv("SANDBOX_IMAGE", "python:3.12-slim"),
            sandbox_network=os.getenv("SANDBOX_NETWORK", "none"),
            sandbox_setup_command=os.getenv("SANDBOX_SETUP_COMMAND"),
            default_test_command=os.getenv("AUTO_TEST_COMMAND"),
            max_review_rounds=_bounded_int("MAX_REVIEW_ROUNDS", "3", minimum=1, maximum=3),
            max_issue_tokens=_optional_int(os.getenv("MAX_ISSUE_TOKENS")),
            max_issue_dollars=_optional_float(os.getenv("MAX_ISSUE_DOLLARS")),
            comment_limit=int(os.getenv("COMMENT_LIMIT_PER_RUN", "2")),
            dry_run=dry_run,
            mock_llm=mock_llm or os.getenv("AUTOBOT_MOCK_LLM") == "1",
        )


def infer_llm_provider(configured: str | None = None) -> str | None:
    if configured:
        return configured
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def configured_llm_models(config: Config) -> list[str]:
    return [
        config.triage_model,
        config.implement_model,
        config.review_model,
        *config.review_models,
    ]


def model_provider_hint(model: str) -> str | None:
    normalized = model.strip().lower()
    if normalized.startswith(OPENAI_MODEL_PREFIXES):
        return "openai"
    if normalized.startswith(ANTHROPIC_MODEL_PREFIXES):
        return "anthropic"
    return None


def provider_for_model(default_provider: str, model: str) -> str:
    return model_provider_hint(model) or default_provider


def missing_model_keys(default_provider: str | None, models: list[str]) -> dict[str, list[str]]:
    if default_provider not in LLM_KEY_ENV:
        return {}
    missing: dict[str, list[str]] = {}
    for model in models:
        provider = provider_for_model(default_provider, model)
        key_name = LLM_KEY_ENV[provider]
        if os.getenv(key_name):
            continue
        missing.setdefault(key_name, [])
        if model not in missing[key_name]:
            missing[key_name].append(model)
    return missing


def missing_model_keys_message(missing: dict[str, list[str]]) -> str:
    parts = [f"{key} for {', '.join(models)}" for key, models in sorted(missing.items())]
    return "missing LLM API key(s) for configured model(s): " + "; ".join(parts)


def model_providers(default_provider: str | None, models: list[str]) -> list[str]:
    if default_provider not in LLM_KEY_ENV:
        return []
    providers = [provider_for_model(default_provider, model) for model in models]
    return list(dict.fromkeys(providers))


def _default_model(provider: str | None) -> str:
    if provider == "anthropic":
        return ANTHROPIC_DEFAULT_MODEL
    return OPENAI_DEFAULT_MODEL


def _optional_int(value: str | None) -> int | None:
    return int(value) if value else None


def _optional_float(value: str | None) -> float | None:
    return float(value) if value else None


def _bounded_int(name: str, default: str, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, default))
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _model_list(value: str | None, fallback: str) -> list[str]:
    if not value:
        return [fallback]
    models = [item.strip() for item in value.split(",") if item.strip()]
    return models or [fallback]
