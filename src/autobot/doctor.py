from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass

from autobot.config import Config, infer_llm_provider
from autobot.github import GitHubIssueTracker
from autobot.sandbox import SandboxError, ensure_no_secret_commands
from autobot.scanner import redact_secret_like_values


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def run_doctor(
    config: Config,
    repo: str | None = None,
    issue: int | None = None,
    network: bool = True,
    command_runner: Callable = subprocess.run,
    tracker_factory: Callable = GitHubIssueTracker,
) -> list[CheckResult]:
    checks = [
        _command_check("git", ["git", "--version"], command_runner),
        _git_identity_check(config, command_runner),
        _docker_check(config, command_runner),
        _github_token_check(config),
        _agent_login_check(config),
        _llm_key_check(config),
        _model_check("triage model", config.triage_model),
        _model_check("implement model", config.implement_model),
        _model_check("review model", config.review_model),
        _sandbox_image_check(config),
        _sandbox_network_check(config),
        _sandbox_setup_check(config),
    ]
    checks.append(_issue_check(config, repo, issue, network, tracker_factory))
    return checks


def doctor_ok(checks: list[CheckResult]) -> bool:
    return not any(check.status == "fail" for check in checks)


def _command_check(name: str, command: list[str], command_runner: Callable) -> CheckResult:
    try:
        result = command_runner(command, capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(name, "fail", str(exc))
    if result.returncode != 0:
        return CheckResult(name, "fail", (result.stderr or result.stdout).strip())
    first_line = (result.stdout or "").splitlines()[0] if result.stdout else "available"
    return CheckResult(name, "pass", first_line)


def _git_identity_check(config: Config, command_runner: Callable) -> CheckResult:
    if config.dry_run:
        return CheckResult("git identity", "skip", "dry-run does not commit")
    if os.getenv("GIT_AUTHOR_NAME") and os.getenv("GIT_AUTHOR_EMAIL"):
        return CheckResult("git identity", "pass", "GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL are set")
    name = _git_config("user.name", command_runner)
    email = _git_config("user.email", command_runner)
    if name and email:
        return CheckResult("git identity", "pass", f"{name} <{email}>")
    return CheckResult("git identity", "fail", "configure git user.name and user.email")


def _git_config(key: str, command_runner: Callable) -> str | None:
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


def _docker_check(config: Config, command_runner: Callable) -> CheckResult:
    if config.dry_run:
        return CheckResult("docker", "skip", "dry-run does not start Docker")
    return _command_check("docker", ["docker", "--version"], command_runner)


def _github_token_check(config: Config) -> CheckResult:
    if config.dry_run:
        return CheckResult("github token", "skip", "dry-run does not need GITHUB_TOKEN")
    if config.github_token:
        return CheckResult("github token", "pass", "GITHUB_TOKEN is set")
    return CheckResult("github token", "fail", "GITHUB_TOKEN is required for live runs")


def _agent_login_check(config: Config) -> CheckResult:
    if config.agent_login:
        return CheckResult("agent login", "pass", "AGENT_LOGIN/GITHUB_ACTOR is set")
    return CheckResult("agent login", "warn", "watch mode works best with AGENT_LOGIN set")


def _llm_key_check(config: Config) -> CheckResult:
    if config.mock_llm or config.dry_run:
        return CheckResult("llm key", "skip", "mock or dry-run mode does not need an LLM key")
    provider = infer_llm_provider(config.llm_provider)
    if provider not in {None, "openai", "anthropic"}:
        return CheckResult("llm key", "fail", "LLM_PROVIDER must be openai or anthropic")
    if provider == "openai":
        return _env_key_check("llm key", "OPENAI_API_KEY")
    if provider == "anthropic":
        return _env_key_check("llm key", "ANTHROPIC_API_KEY")
    if os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"):
        return CheckResult("llm key", "pass", "an OpenAI or Anthropic key is set")
    return CheckResult("llm key", "fail", "OPENAI_API_KEY or ANTHROPIC_API_KEY is required")


def _env_key_check(name: str, key: str) -> CheckResult:
    if os.getenv(key):
        return CheckResult(name, "pass", f"{key} is set")
    return CheckResult(name, "fail", f"{key} is required")


def _model_check(name: str, model: str) -> CheckResult:
    if model:
        return CheckResult(name, "pass", model)
    return CheckResult(name, "fail", f"{name} is empty")


def _sandbox_image_check(config: Config) -> CheckResult:
    if config.dry_run:
        return CheckResult("sandbox image", "skip", "dry-run does not start Docker")
    if config.sandbox_image:
        return CheckResult("sandbox image", "pass", config.sandbox_image)
    return CheckResult("sandbox image", "fail", "SANDBOX_IMAGE must not be empty")


def _sandbox_network_check(config: Config) -> CheckResult:
    if config.dry_run:
        return CheckResult("sandbox network", "skip", "dry-run does not start Docker")
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
    tracker_factory: Callable,
) -> CheckResult:
    if not repo or issue is None:
        return CheckResult("issue readable", "skip", "provide --repo and --issue to check GitHub")
    if not network:
        return CheckResult("issue readable", "skip", "network check disabled")
    if not config.github_token and not config.dry_run:
        return CheckResult("issue readable", "skip", "GITHUB_TOKEN missing")
    try:
        issue_data = tracker_factory(config.github_token, config.agent_login).get(repo, issue)
    except Exception as exc:
        return CheckResult("issue readable", "fail", redact_secret_like_values(str(exc)))
    return CheckResult("issue readable", "pass", f"{issue_data.repo}#{issue_data.number}")
