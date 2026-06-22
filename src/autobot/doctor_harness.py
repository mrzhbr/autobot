from __future__ import annotations

import os
import subprocess

from autobot.config import LLM_KEY_ENV, Config
from autobot.doctor_result import CheckResult, CommandRunner


def implementation_harness_check(config: Config, command_runner: CommandRunner) -> CheckResult:
    if config.dry_run or config.mock_llm:
        return CheckResult(
            "implementation harness",
            "skip",
            "dry-run or mock mode uses the legacy mock harness",
        )
    if config.implement_harness == "legacy":
        return CheckResult("implementation harness", "pass", "legacy")
    if config.implement_harness == "openhands":
        return CheckResult("implementation harness", "fail", "OpenHands adapter is not wired yet")
    if config.implement_harness != "pi":
        return CheckResult("implementation harness", "fail", "unknown implementation harness")
    provider = config.harness_llm_provider
    key = LLM_KEY_ENV.get(provider or "")
    if not key:
        return CheckResult(
            "implementation harness",
            "fail",
            "HARNESS_LLM_PROVIDER must be openai, anthropic, or openrouter",
        )
    if not os.getenv(key):
        return CheckResult("implementation harness", "fail", f"{key} is required for Pi harness")
    if config.sandbox_network == "none":
        return CheckResult(
            "implementation harness",
            "fail",
            "Pi harness runs inside Docker and requires SANDBOX_NETWORK with egress",
        )
    return _pi_cli_check(config, command_runner, "implementation harness", config.harness_model)


def planner_harness_check(config: Config, command_runner: CommandRunner) -> CheckResult:
    if not config.planner_enabled:
        return CheckResult("planner harness", "skip", "planner disabled")
    if config.dry_run or config.mock_llm:
        return CheckResult(
            "planner harness",
            "skip",
            "dry-run or mock mode uses the legacy mock harness",
        )
    if config.planner_harness != "pi":
        return CheckResult("planner harness", "fail", "unknown planner harness")
    provider = config.planner_llm_provider
    key = LLM_KEY_ENV.get(provider or "")
    if not key:
        return CheckResult(
            "planner harness",
            "fail",
            "PLANNER_LLM_PROVIDER must be openai, anthropic, or openrouter",
        )
    if not os.getenv(key):
        return CheckResult("planner harness", "fail", f"{key} is required for Pi planner")
    if config.sandbox_network == "none":
        return CheckResult(
            "planner harness",
            "fail",
            "Pi planner runs inside Docker and requires SANDBOX_NETWORK with egress",
        )
    return _pi_cli_check(config, command_runner, "planner harness", config.planner_model)


def _pi_cli_check(
    config: Config,
    command_runner: CommandRunner,
    name: str,
    model: str,
) -> CheckResult:
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        config.sandbox_network,
        config.sandbox_image,
        "sh",
        "-lc",
        "PI_CODING_AGENT_DIR=/tmp/autobot-pi-agent PI_OFFLINE=1 pi --version",
    ]
    try:
        result = command_runner(command, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(name, "fail", str(exc))
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        return CheckResult(
            name,
            "fail",
            "Pi CLI is not available in SANDBOX_IMAGE: " + output,
        )
    version_text = (result.stdout or result.stderr or "").strip()
    version = version_text.splitlines()[0] if version_text else "available"
    return CheckResult(name, "pass", f"pi {version} using {model}")
