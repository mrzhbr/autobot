from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from autobot.config import LLM_KEY_ENV, OPENROUTER_MODEL_PREFIX, Config
from autobot.harness import (
    HarnessError,
    HarnessResult,
    HarnessSession,
    HarnessTask,
    HarnessTaskKind,
    PlanningResult,
)
from autobot.models import Usage
from autobot.sandbox import DockerSandbox
from autobot.scanner import redact_secret_like_values


class PiHarnessAdapter:
    def __init__(self, config: Config) -> None:
        self.config = config

    def start(
        self,
        repo_dir: Path,
        sandbox: DockerSandbox | None = None,
        dry_run: bool = False,
    ) -> HarnessSession:
        if dry_run or self.config.mock_llm:
            raise HarnessError("Pi harness is not used in dry-run or mock LLM mode")
        if sandbox is None:
            raise HarnessError("Pi harness requires a live Docker sandbox")
        provider = self.config.harness_llm_provider
        if not provider:
            raise HarnessError(
                "HARNESS_LLM_PROVIDER or a provider-routed HARNESS_MODEL is required"
            )
        process = sandbox.popen(
            _pi_command(provider, self.config.harness_model, writable=True),
            env={
                "PI_CODING_AGENT_DIR": "/tmp/autobot-pi-agent",
                "PI_OFFLINE": "1",
            },
        )
        return PiHarnessSession(
            self.config,
            repo_dir,
            process,
            transcript_dir=repo_dir.parent / "harness",
            model=self.config.harness_model,
        )

    def start_planner(
        self,
        repo_dir: Path,
        sandbox: DockerSandbox,
    ) -> PiHarnessSession:
        provider = self.config.planner_llm_provider
        if not provider:
            raise HarnessError(
                "PLANNER_LLM_PROVIDER or a provider-routed PLANNER_MODEL is required"
            )
        process = sandbox.popen(
            _pi_command(provider, self.config.planner_model, writable=False),
            env={
                "PI_CODING_AGENT_DIR": "/tmp/autobot-pi-agent",
                "PI_OFFLINE": "1",
            },
        )
        return PiHarnessSession(
            self.config,
            repo_dir,
            process,
            transcript_dir=repo_dir.parent / "harness",
            model=self.config.planner_model,
        )


class PiHarnessSession:
    def __init__(
        self,
        config: Config,
        repo_dir: Path,
        process: subprocess.Popen[str],
        transcript_dir: Path,
        model: str | None = None,
    ) -> None:
        self.config = config
        self.repo_dir = repo_dir
        self.process = process
        self.transcript_dir = transcript_dir
        self.model = model or config.harness_model
        self._stdout: queue.Queue[str | None] = queue.Queue()
        self._stderr: list[str] = []
        self._last_usage: dict[str, float] = {}
        threading.Thread(target=_read_stdout, args=(process, self._stdout), daemon=True).start()
        threading.Thread(target=_read_stderr, args=(process, self._stderr), daemon=True).start()

    def plan(self, task: HarnessTask) -> PlanningResult:
        transcript: list[dict[str, Any]] = []
        prompt_id = _request_id(task.kind.value)
        self._send({"id": prompt_id, "type": "prompt", "message": _planner_prompt(task)})
        self._wait_for_response(prompt_id, transcript)
        self._wait_for_agent_end(transcript)
        text = self._last_assistant_text(transcript)
        payload = _parse_json_object(text or "{}")
        transcript_path = self._write_transcript(task, transcript, text)
        return PlanningResult(
            summary=_string(payload.get("summary")),
            target_files=_string_list(payload.get("target_files")),
            constraints=_string_list(payload.get("constraints")),
            implementation_steps=_string_list(payload.get("implementation_steps")),
            tests_to_add=_string_list(payload.get("tests_to_add")),
            verification_commands=_string_list(payload.get("verification_commands")),
            risks=_string_list(payload.get("risks")),
            non_goals=_string_list(payload.get("non_goals")),
            usage=self._usage_since_last(task.kind.value, transcript),
            transcript_path=str(transcript_path),
        )

    def run(self, task: HarnessTask) -> HarnessResult:
        before = _git_status_paths(self.repo_dir)
        transcript: list[dict[str, Any]] = []
        prompt_id = _request_id(task.kind.value)
        self._send({"id": prompt_id, "type": "prompt", "message": _task_prompt(task)})
        self._wait_for_response(prompt_id, transcript)
        self._wait_for_agent_end(transcript)
        text = self._last_assistant_text(transcript)
        payload = _parse_json_object(text or "{}")
        after = _git_status_paths(self.repo_dir)
        changed_paths = _changed_paths(payload, after, before)
        transcript_path = self._write_transcript(task, transcript, text)
        return HarnessResult(
            plan=_string_list(payload.get("plan")),
            changes=[],
            test_commands=_string_list(payload.get("test_commands")),
            usage=self._usage_since_last(task.kind.value, transcript),
            applied_in_workspace=True,
            changed_paths=changed_paths,
            transcript_path=str(transcript_path),
        )

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise HarnessError("Pi RPC stdin is not available")
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def _wait_for_response(
        self, request_id: str, transcript: list[dict[str, Any]]
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.harness_timeout_seconds
        while True:
            event = self._read_event(deadline)
            transcript.append(event)
            if event.get("type") != "response" or event.get("id") != request_id:
                continue
            if event.get("success"):
                return event
            message = event.get("error") or event.get("message") or event
            raise HarnessError("Pi RPC command failed: " + redact_secret_like_values(str(message)))

    def _wait_for_agent_end(self, transcript: list[dict[str, Any]]) -> None:
        deadline = time.monotonic() + self.config.harness_timeout_seconds
        while True:
            event = self._read_event(deadline)
            transcript.append(event)
            if event.get("type") == "agent_end":
                return

    def _last_assistant_text(self, transcript: list[dict[str, Any]]) -> str | None:
        request_id = _request_id("last-text")
        self._send({"id": request_id, "type": "get_last_assistant_text"})
        response = self._wait_for_response(request_id, transcript)
        data = response.get("data")
        if isinstance(data, dict):
            text = data.get("text")
            if isinstance(text, str):
                return text
        return None

    def _usage_since_last(self, role: str, transcript: list[dict[str, Any]]) -> Usage | None:
        request_id = _request_id("stats")
        self._send({"id": request_id, "type": "get_session_stats"})
        response = self._wait_for_response(request_id, transcript)
        data = response.get("data")
        if not isinstance(data, dict):
            return None
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            return None
        current = {
            "input": float(tokens.get("input") or 0),
            "output": float(tokens.get("output") or 0),
            "cost": float(data.get("cost") or 0),
        }
        previous = self._last_usage
        self._last_usage = current
        return Usage(
            role=role,
            model=self.model,
            input_tokens=max(0, int(current["input"] - previous.get("input", 0))),
            output_tokens=max(0, int(current["output"] - previous.get("output", 0))),
            dollars=max(0.0, current["cost"] - previous.get("cost", 0)),
        )

    def _read_event(self, deadline: float) -> dict[str, Any]:
        timeout = max(0.0, deadline - time.monotonic())
        if timeout == 0:
            raise HarnessError("Pi RPC timed out")
        line = self._stdout.get(timeout=timeout)
        if line is None:
            stderr = redact_secret_like_values("\n".join(self._stderr[-20:]))
            raise HarnessError(
                "Pi RPC exited before completing task" + (f": {stderr}" if stderr else "")
            )
        try:
            data: Any = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HarnessError(
                "Pi RPC emitted non-JSON output: " + redact_secret_like_values(line)
            ) from exc
        if not isinstance(data, dict):
            raise HarnessError(
                "Pi RPC emitted non-object JSON output: " + redact_secret_like_values(line)
            )
        return data

    def _write_transcript(
        self,
        task: HarnessTask,
        transcript: list[dict[str, Any]],
        assistant_text: str | None,
    ) -> Path:
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        path = self.transcript_dir / f"pi-{task.kind.value}-{uuid4().hex}.jsonl"
        rows = [*transcript, {"type": "autobot_assistant_text", "text": assistant_text}]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path


def pi_container_env_names(config: Config) -> list[str]:
    if config.dry_run or config.mock_llm:
        return []
    if config.implement_harness != "pi" and not config.planner_enabled:
        return []
    providers = [config.harness_llm_provider]
    if config.planner_enabled:
        providers.append(config.planner_llm_provider)
    keys = [LLM_KEY_ENV.get(provider or "") for provider in providers]
    return list(dict.fromkeys(key for key in keys if key))


def _pi_command(provider: str, model: str, writable: bool) -> list[str]:
    tools = "read,grep,find,ls,bash"
    if writable:
        tools += ",edit,write"
    return [
        "pi",
        "--mode",
        "rpc",
        "--no-session",
        "--provider",
        provider,
        "--model",
        _provider_model(provider, model),
        "--tools",
        tools,
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--offline",
    ]


def _provider_model(provider: str, model: str) -> str:
    if provider == "openrouter" and model.lower().startswith(OPENROUTER_MODEL_PREFIX):
        return model[len(OPENROUTER_MODEL_PREFIX) :]
    return model


def _task_prompt(task: HarnessTask) -> str:
    feedback = "\n".join(task.review_findings) or "None"
    feedback_label = "Fix feedback"
    if task.kind == HarnessTaskKind.REVIEW_FIX:
        feedback_label = "Blocking reviewer findings"
    elif task.kind == HarnessTaskKind.VERIFICATION_FIX:
        feedback_label = "Verification failure output"
    context = "\n\n".join(f"## {item.path}\n{item.content}" for item in task.context)
    planner = task.planning_context or "None"
    return (
        "You are Autobot's implementation harness. Modify files directly in this repository. "
        "Keep changes scoped to the issue. Run only necessary local inspection commands. "
        "Do not create commits, branches, pull requests, comments, or network calls except "
        "LLM calls. "
        "When finished, respond with one JSON object and no extra prose: "
        '{"plan":["..."],"test_commands":["..."],"changed_paths":["..."]}.\n\n'
        f"Task kind: {task.kind.value}\n"
        f"Issue #{task.issue.number}: {task.issue.title}\n\n{task.issue.body}\n\n"
        f"Planner output:\n{planner}\n\n"
        f"{feedback_label}:\n{feedback}\n\n"
        f"Repo context:\n{context or 'No focused context gathered.'}"
    )


def _planner_prompt(task: HarnessTask) -> str:
    context = "\n\n".join(f"## {item.path}\n{item.content}" for item in task.context)
    return (
        "You are Autobot's read-only planning agent. Inspect the repository freely using "
        "read-only tools and shell commands that do not modify files. Do not edit files, "
        "create commits, push branches, open pull requests, or leave generated artifacts. "
        "Your job is to produce an implementation strategy for a cheaper implementer. "
        "Return one JSON object and no prose with keys: summary string, target_files array "
        "of strings, constraints array of strings, implementation_steps array of strings, "
        "tests_to_add array of strings, verification_commands array of strings, risks array "
        "of strings, non_goals array of strings.\n\n"
        f"Issue #{task.issue.number}: {task.issue.title}\n\n{task.issue.body}\n\n"
        f"Initial repo context:\n{context or 'No focused context gathered. Inspect the repo.'}"
    )


def _git_status_paths(repo_dir: Path) -> set[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise HarnessError(redact_secret_like_values(result.stderr.strip() or "git status failed"))
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            paths.add(path)
    return paths


def _changed_paths(payload: dict[str, Any], after: set[str], before: set[str]) -> list[str]:
    declared = _string_list(payload.get("changed_paths"))
    if declared:
        return list(dict.fromkeys(declared))
    delta = sorted(after - before)
    return delta or sorted(after)


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {}
        data = json.loads(match.group(0))
    return data if isinstance(data, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _request_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _read_stdout(process: subprocess.Popen[str], output: queue.Queue[str | None]) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        output.put(line.rstrip("\n").rstrip("\r"))
    output.put(None)


def _read_stderr(process: subprocess.Popen[str], output: list[str]) -> None:
    assert process.stderr is not None
    for line in process.stderr:
        output.append(line.rstrip("\n").rstrip("\r"))
