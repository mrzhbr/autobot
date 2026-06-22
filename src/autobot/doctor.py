from __future__ import annotations

import os
import subprocess
from typing import Protocol, cast

from autobot.config import (
    LLM_KEY_ENV,
    Config,
    configured_llm_models,
    infer_llm_provider,
    invalid_price_vars,
    missing_model_keys,
    missing_model_keys_message,
    missing_price_vars,
    model_providers,
)
from autobot.doctor_harness import implementation_harness_check, planner_harness_check
from autobot.doctor_result import CheckResult, CommandRunner
from autobot.github import GitHubIssueTracker
from autobot.linear import LinearIssueTracker
from autobot.models import Issue
from autobot.sandbox import SandboxError, ensure_no_secret_commands
from autobot.scanner import redact_secret_like_values


class IssueReader(Protocol):
    def get(self, repo: str, issue: int) -> Issue:
        """Read one issue."""


class IssueTrackerFactory(Protocol):
    def __call__(self, token: str | None, agent_login: str | None) -> IssueReader:
        """Build an issue reader."""


def run_doctor(
    config: Config,
    repo: str | None = None,
    issue: int | None = None,
    network: bool = True,
    command_runner: CommandRunner | None = None,
    tracker_factory: IssueTrackerFactory | None = None,
) -> list[CheckResult]:
    runner = command_runner or cast(CommandRunner, subprocess.run)
    checks = [
        _command_check("git", ["git", "--version"], runner),
        _git_identity_check(config, runner),
        _docker_check(config, runner),
        _github_token_check(config),
        _issue_tracker_check(config),
        _agent_login_check(config),
        _llm_key_check(config),
        _model_check("triage model", config.triage_model),
        _model_check("implement model", config.implement_model),
        _model_check("review model", config.review_model),
        _planner_model_check(config),
        _llm_model_provider_check(config),
        _llm_pricing_check(config),
        implementation_harness_check(config, runner),
        planner_harness_check(config, runner),
        _sandbox_backend_check(config),
        _sandbox_image_check(config),
        _sandbox_network_check(config),
        _sandbox_setup_check(config),
    ]
    checks.append(_issue_check(config, repo, issue, network, tracker_factory))
    return checks


def doctor_ok(checks: list[CheckResult]) -> bool:
    return not any(check.status == "fail" for check in checks)


def _command_check(name: str, command: list[str], command_runner: CommandRunner) -> CheckResult:
    try:
        result = command_runner(command, capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(name, "fail", redact_secret_like_values(str(exc)))
    if result.returncode != 0:
        return CheckResult(
            name,
            "fail",
            redact_secret_like_values((result.stderr or result.stdout).strip()),
        )
    first_line = (result.stdout or "").splitlines()[0] if result.stdout else "available"
    return CheckResult(name, "pass", redact_secret_like_values(first_line))


def _git_identity_check(config: Config, command_runner: CommandRunner) -> CheckResult:
    if config.dry_run:
        return CheckResult("git identity", "skip", "dry-run does not commit")
    if os.getenv("GIT_AUTHOR_NAME") and os.getenv("GIT_AUTHOR_EMAIL"):
        return CheckResult("git identity", "pass", "GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL are set")
    name = _git_config("user.name", command_runner)
    email = _git_config("user.email", command_runner)
    if name and email:
        return CheckResult("git identity", "pass", f"{name} <{email}>")
    return CheckResult("git identity", "fail", "configure git user.name and user.email")


def _git_config(key: str, command_runner: CommandRunner) -> str | None:
    try:
        result = command_runner(
            ["git", "config", "--get", key],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _docker_check(config: Config, command_runner: CommandRunner) -> CheckResult:
    if config.dry_run:
        return CheckResult("docker", "skip", "dry-run does not start Docker")
    cli = _command_check("docker", ["docker", "--version"], command_runner)
    if cli.status == "fail":
        return cli
    daemon = _command_check(
        "docker",
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        command_runner,
    )
    if daemon.status == "fail":
        return CheckResult("docker", "fail", "docker daemon unavailable: " + daemon.message)
    return CheckResult("docker", "pass", f"{cli.message}; daemon {daemon.message}")


def _github_token_check(config: Config) -> CheckResult:
    if config.dry_run:
        return CheckResult("github token", "skip", "dry-run does not need GITHUB_TOKEN")
    if config.github_token:
        return CheckResult("github token", "pass", "GITHUB_TOKEN is set")
    return CheckResult("github token", "fail", "GITHUB_TOKEN is required for live runs and PRs")


def _issue_tracker_check(config: Config) -> CheckResult:
    if config.issue_tracker == "github":
        return CheckResult("issue tracker", "pass", "github")
    if config.issue_tracker != "linear":
        return CheckResult("issue tracker", "fail", "ISSUE_TRACKER must be github or linear")
    if config.dry_run:
        return CheckResult("issue tracker", "skip", "dry-run does not need LINEAR_API_KEY")
    if not config.linear_api_key:
        return CheckResult("issue tracker", "fail", "LINEAR_API_KEY is required")
    if not config.linear_team_key:
        return CheckResult("issue tracker", "fail", "LINEAR_TEAM_KEY is required")
    return CheckResult("issue tracker", "pass", f"linear team {config.linear_team_key}")


def _agent_login_check(config: Config) -> CheckResult:
    login = config.linear_agent_login if config.issue_tracker == "linear" else config.agent_login
    if login:
        return CheckResult("agent login", "pass", "agent login is set")
    return CheckResult("agent login", "warn", "watch mode works best with AGENT_LOGIN set")


def _llm_key_check(config: Config) -> CheckResult:
    if config.mock_llm or config.dry_run:
        return CheckResult("llm key", "skip", "mock or dry-run mode does not need an LLM key")
    provider = infer_llm_provider(config.llm_provider)
    if provider not in {None, *LLM_KEY_ENV}:
        return CheckResult(
            "llm key", "fail", "LLM_PROVIDER must be openai, anthropic, or openrouter"
        )
    if provider:
        return _env_key_check("llm key", LLM_KEY_ENV[provider])
    if any(os.getenv(key) for key in LLM_KEY_ENV.values()):
        return CheckResult("llm key", "pass", "an OpenAI, Anthropic, or OpenRouter key is set")
    return CheckResult(
        "llm key",
        "fail",
        "OPENAI_API_KEY, ANTHROPIC_API_KEY, or OPENROUTER_API_KEY is required",
    )


def _env_key_check(name: str, key: str) -> CheckResult:
    if os.getenv(key):
        return CheckResult(name, "pass", f"{key} is set")
    return CheckResult(name, "fail", f"{key} is required")


def _model_check(name: str, model: str) -> CheckResult:
    if model:
        return CheckResult(name, "pass", model)
    return CheckResult(name, "fail", f"{name} is empty")


def _planner_model_check(config: Config) -> CheckResult:
    if not config.planner_enabled:
        return CheckResult("planner model", "skip", "planner disabled")
    return _model_check("planner model", config.planner_model)


def _llm_model_provider_check(config: Config) -> CheckResult:
    if config.mock_llm or config.dry_run:
        return CheckResult(
            "llm model/provider", "skip", "mock or dry-run mode does not call a provider"
        )
    provider = infer_llm_provider(config.llm_provider)
    if provider is None:
        return CheckResult("llm model/provider", "skip", "LLM key missing")
    if provider not in LLM_KEY_ENV:
        return CheckResult("llm model/provider", "skip", "valid LLM_PROVIDER required")
    models = configured_llm_models(config)
    missing = missing_model_keys(provider, models)
    if missing:
        return CheckResult(
            "llm model/provider",
            "fail",
            missing_model_keys_message(missing),
        )
    providers = ", ".join(model_providers(provider, models))
    return CheckResult("llm model/provider", "pass", f"model providers available: {providers}")


def _llm_pricing_check(config: Config) -> CheckResult:
    if config.mock_llm or config.dry_run:
        return CheckResult("llm pricing", "skip", "mock or dry-run mode reports zero dollars")
    provider = infer_llm_provider(config.llm_provider)
    if provider not in LLM_KEY_ENV:
        return CheckResult("llm pricing", "skip", "valid LLM_PROVIDER required")
    invalid = invalid_price_vars()
    if invalid:
        return CheckResult(
            "llm pricing",
            "fail",
            "LLM pricing env vars must be numeric: " + ", ".join(invalid),
        )
    missing = missing_price_vars()
    if missing:
        if config.max_issue_dollars is not None:
            return CheckResult(
                "llm pricing",
                "fail",
                "MAX_ISSUE_DOLLARS requires LLM pricing env vars: " + ", ".join(missing),
            )
        return CheckResult(
            "llm pricing",
            "warn",
            "dollars will be reported as not configured; missing " + ", ".join(missing),
        )
    return CheckResult(
        "llm pricing", "pass", "triage, implement, test, and review prices configured"
    )


def _sandbox_image_check(config: Config) -> CheckResult:
    if config.dry_run:
        return CheckResult("sandbox image", "skip", "dry-run does not start Docker")
    if config.sandbox_image:
        return CheckResult("sandbox image", "pass", config.sandbox_image)
    return CheckResult("sandbox image", "fail", "SANDBOX_IMAGE must not be empty")


def _sandbox_backend_check(config: Config) -> CheckResult:
    if config.dry_run:
        return CheckResult("sandbox backend", "skip", "dry-run does not start Docker")
    if config.sandbox_backend == "docker-copy":
        return CheckResult(
            "sandbox backend",
            "pass",
            "docker-copy uses an isolated container workspace and syncs changed paths",
        )
    if config.sandbox_backend == "docker-bind":
        return CheckResult(
            "sandbox backend",
            "warn",
            "docker-bind exposes the host clone through a writable bind mount",
        )
    return CheckResult(
        "sandbox backend",
        "fail",
        "SANDBOX_BACKEND must be docker-bind or docker-copy",
    )


def _sandbox_network_check(config: Config) -> CheckResult:
    if config.dry_run:
        return CheckResult("sandbox network", "skip", "dry-run does not start Docker")
    if not config.sandbox_network.strip():
        return CheckResult("sandbox network", "fail", "SANDBOX_NETWORK must not be empty")
    if config.sandbox_network == "none":
        return CheckResult("sandbox network", "pass", "none")
    return CheckResult(
        "sandbox network",
        "warn",
        f"{config.sandbox_network} allows container egress; use only when setup/tests need it",
    )


def _sandbox_setup_check(config: Config) -> CheckResult:
    if config.dry_run:
        return CheckResult("sandbox setup", "skip", "dry-run does not start Docker")
    if not config.sandbox_setup_command:
        return CheckResult(
            "sandbox setup", "warn", "setup command will be auto-detected after clone"
        )
    try:
        ensure_no_secret_commands([config.sandbox_setup_command], "sandbox setup command")
    except SandboxError as exc:
        return CheckResult("sandbox setup", "fail", str(exc))
    return CheckResult("sandbox setup", "pass", config.sandbox_setup_command)


def _issue_check(
    config: Config,
    repo: str | None,
    issue: int | None,
    network: bool,
    tracker_factory: IssueTrackerFactory | None,
) -> CheckResult:
    if not repo or issue is None:
        return CheckResult("issue readable", "skip", "provide --repo and --issue to check")
    if not network:
        return CheckResult("issue readable", "skip", "network check disabled")
    if config.issue_tracker == "github" and not config.github_token and not config.dry_run:
        return CheckResult("issue readable", "skip", "GITHUB_TOKEN missing")
    if config.issue_tracker == "linear" and not config.linear_api_key and not config.dry_run:
        return CheckResult("issue readable", "skip", "LINEAR_API_KEY missing")
    try:
        if tracker_factory is not None:
            reader = tracker_factory(config.github_token, config.agent_login)
        elif config.issue_tracker == "linear":
            reader = LinearIssueTracker(
                config.linear_api_key,
                config.linear_agent_login,
                config.linear_team_key,
            )
        else:
            reader = GitHubIssueTracker(config.github_token, config.agent_login)
        issue_data = reader.get(repo, issue)
    except Exception as exc:
        return CheckResult("issue readable", "fail", redact_secret_like_values(str(exc)))
    return CheckResult("issue readable", "pass", f"{issue_data.repo}#{issue_data.number}")
