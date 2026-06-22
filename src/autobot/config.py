from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

OPENAI_DEFAULT_MODEL = "gpt-4.1"
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-20250514"
OPENROUTER_DEFAULT_MODEL = "openai/gpt-4.1"
OPENAI_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4")
ANTHROPIC_MODEL_PREFIXES = ("claude-",)
OPENROUTER_MODEL_PREFIX = "openrouter/"
LLM_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
PRICE_ROLES = ("TRIAGE", "IMPLEMENT", "TEST", "REVIEW")
PRICE_DIRECTIONS = ("INPUT", "OUTPUT")
IMPLEMENT_HARNESSES = ("legacy", "pi", "openhands")
PLANNER_HARNESSES = ("pi",)
SANDBOX_BACKENDS = ("docker-bind", "docker-copy")
ISSUE_TRACKERS = ("github", "linear")


@dataclass(frozen=True)
class Config:
    root: Path
    db_path: Path
    audit_path: Path
    work_root: Path
    github_token: str | None
    github_app_id: str | None
    github_app_installation_id: str | None
    github_app_private_key_path: Path | None
    agent_login: str | None
    llm_provider: str | None
    triage_model: str
    implement_model: str
    review_model: str
    review_models: list[str]
    sandbox_backend: str
    sandbox_image: str
    sandbox_network: str
    sandbox_setup_command: str | None
    default_test_command: str | None
    max_review_rounds: int
    max_issue_tokens: int | None
    max_issue_dollars: float | None
    comment_limit: int
    implement_harness: str
    harness_llm_provider: str | None
    harness_model: str
    harness_timeout_seconds: int
    harness_max_restarts: int
    planner_enabled: bool
    planner_harness: str
    planner_llm_provider: str | None
    planner_model: str
    dry_run: bool = False
    mock_llm: bool = False
    issue_tracker: str = "github"
    linear_api_key: str | None = None
    linear_team_key: str | None = None
    linear_agent_login: str | None = None

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
        triage_model = os.getenv("TRIAGE_MODEL", model)
        implement_model = os.getenv("IMPLEMENT_MODEL", model)
        review_model = os.getenv("REVIEW_MODEL", model)
        harness_model = os.getenv("HARNESS_MODEL", implement_model)
        planner_model = os.getenv("PLANNER_MODEL", review_model)
        harness_provider = (
            os.getenv("HARNESS_LLM_PROVIDER")
            or model_provider_hint(harness_model)
            or infer_llm_provider(provider)
        )
        planner_provider = (
            os.getenv("PLANNER_LLM_PROVIDER")
            or model_provider_hint(planner_model)
            or harness_provider
            or infer_llm_provider(provider)
        )
        db_default = os.getenv("AUTOBOT_DB", str(base / "state.db"))
        audit_default = os.getenv("AUTOBOT_AUDIT_LOG", str(base / "audit.jsonl"))
        work_default = os.getenv("AUTOBOT_WORK_ROOT", str(base / "work"))
        return cls(
            root=root,
            db_path=Path(db_path) if db_path else Path(db_default),
            audit_path=Path(audit_default),
            work_root=Path(work_root) if work_root else Path(work_default),
            github_token=os.getenv("GITHUB_TOKEN"),
            github_app_id=os.getenv("GITHUB_APP_ID"),
            github_app_installation_id=os.getenv("GITHUB_APP_INSTALLATION_ID"),
            github_app_private_key_path=_optional_path("GITHUB_APP_PRIVATE_KEY_PATH"),
            agent_login=os.getenv("AGENT_LOGIN") or os.getenv("GITHUB_ACTOR"),
            llm_provider=provider,
            triage_model=triage_model,
            implement_model=implement_model,
            review_model=review_model,
            review_models=_model_list(os.getenv("REVIEW_MODELS"), review_model),
            sandbox_backend=_sandbox_backend(),
            sandbox_image=os.getenv("SANDBOX_IMAGE", "python:3.12-slim"),
            sandbox_network=os.getenv("SANDBOX_NETWORK", "none"),
            sandbox_setup_command=os.getenv("SANDBOX_SETUP_COMMAND"),
            default_test_command=os.getenv("AUTO_TEST_COMMAND"),
            max_review_rounds=_bounded_int("MAX_REVIEW_ROUNDS", "3", minimum=1, maximum=3),
            max_issue_tokens=_optional_nonnegative_int("MAX_ISSUE_TOKENS"),
            max_issue_dollars=_optional_nonnegative_float("MAX_ISSUE_DOLLARS"),
            comment_limit=_bounded_int("COMMENT_LIMIT_PER_RUN", "2", minimum=0),
            implement_harness=_implement_harness(),
            harness_llm_provider=harness_provider,
            harness_model=harness_model,
            harness_timeout_seconds=_bounded_int("HARNESS_TIMEOUT_SECONDS", "1800", minimum=1),
            harness_max_restarts=_bounded_int("HARNESS_MAX_RESTARTS", "1", minimum=0, maximum=3),
            planner_enabled=_env_bool("PLANNER_ENABLED", default=False),
            planner_harness=_planner_harness(),
            planner_llm_provider=planner_provider,
            planner_model=planner_model,
            dry_run=dry_run,
            mock_llm=mock_llm or os.getenv("AUTOBOT_MOCK_LLM") == "1",
            issue_tracker=_issue_tracker(),
            linear_api_key=os.getenv("LINEAR_API_KEY"),
            linear_team_key=os.getenv("LINEAR_TEAM_KEY"),
            linear_agent_login=os.getenv("LINEAR_AGENT_LOGIN")
            or os.getenv("AGENT_LOGIN")
            or os.getenv("GITHUB_ACTOR"),
        )


def infer_llm_provider(configured: str | None = None) -> str | None:
    if configured:
        return configured.strip().lower()
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OPENROUTER_API_KEY"):
        return "openrouter"
    return None


def configured_llm_models(config: Config) -> list[str]:
    models = [
        config.triage_model,
        config.implement_model,
        config.review_model,
        *config.review_models,
    ]
    if config.planner_enabled:
        models.append(config.planner_model)
    return models


def model_provider_hint(model: str) -> str | None:
    normalized = model.strip().lower()
    if normalized.startswith(OPENROUTER_MODEL_PREFIX):
        return "openrouter"
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


def missing_price_vars() -> list[str]:
    missing: list[str] = []
    for role in ("TRIAGE", "IMPLEMENT", "REVIEW"):
        missing.extend(missing_role_price_vars(role))
    if missing_role_price_vars("TEST") and missing_role_price_vars("IMPLEMENT"):
        missing.extend(missing_role_price_vars("TEST"))
    return missing


def missing_role_price_vars(role: str) -> list[str]:
    return [name for name in role_price_var_names(role) if not os.getenv(name)]


def invalid_price_vars() -> list[str]:
    invalid = []
    for role in PRICE_ROLES:
        for name in role_price_var_names(role):
            value = os.getenv(name)
            if not value:
                continue
            try:
                float(value)
            except ValueError:
                invalid.append(name)
    return invalid


def role_price_var_names(role: str) -> list[str]:
    return [f"{role}_{direction}_PRICE_PER_1K" for direction in PRICE_DIRECTIONS]


def _implement_harness() -> str:
    value = os.getenv("IMPLEMENT_HARNESS", "legacy").strip().lower()
    if value not in IMPLEMENT_HARNESSES:
        joined = ", ".join(IMPLEMENT_HARNESSES)
        raise ValueError(f"IMPLEMENT_HARNESS must be one of: {joined}")
    return value


def _planner_harness() -> str:
    value = os.getenv("PLANNER_HARNESS", "pi").strip().lower()
    if value not in PLANNER_HARNESSES:
        joined = ", ".join(PLANNER_HARNESSES)
        raise ValueError(f"PLANNER_HARNESS must be one of: {joined}")
    return value


def _sandbox_backend() -> str:
    value = os.getenv("SANDBOX_BACKEND", "docker-bind").strip().lower()
    if value not in SANDBOX_BACKENDS:
        joined = ", ".join(SANDBOX_BACKENDS)
        raise ValueError(f"SANDBOX_BACKEND must be one of: {joined}")
    return value


def _issue_tracker() -> str:
    value = os.getenv("ISSUE_TRACKER", "github").strip().lower()
    if value not in ISSUE_TRACKERS:
        joined = ", ".join(ISSUE_TRACKERS)
        raise ValueError(f"ISSUE_TRACKER must be one of: {joined}")
    return value


def price_value(name: str) -> float | None:
    value = os.getenv(name)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _default_model(provider: str | None) -> str:
    if provider == "anthropic":
        return ANTHROPIC_DEFAULT_MODEL
    if provider == "openrouter":
        return OPENROUTER_DEFAULT_MODEL
    return OPENAI_DEFAULT_MODEL


def _optional_nonnegative_int(name: str) -> int | None:
    value = os.getenv(name)
    if not value:
        return None
    parsed = _parse_int(name, value)
    if parsed < 0:
        raise ValueError(f"{name} must be nonnegative")
    return parsed


def _optional_path(name: str) -> Path | None:
    value = os.getenv(name)
    if value in (None, ""):
        return None
    return Path(value).expanduser()


def _optional_nonnegative_float(name: str) -> float | None:
    value = os.getenv(name)
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be nonnegative")
    return parsed


def _bounded_int(
    name: str,
    default: str,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = _parse_int(name, os.getenv(name, default))
    if value < minimum:
        if maximum is None:
            raise ValueError(f"{name} must be at least {minimum}")
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _parse_int(name: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _model_list(value: str | None, fallback: str) -> list[str]:
    if not value:
        return [fallback]
    models = [item.strip() for item in value.split(",") if item.strip()]
    return models or [fallback]


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")
