from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any

from autobot.config import Config
from autobot.context import format_context
from autobot.models import (
    ContextFile,
    FileChange,
    ImplementationPlan,
    Issue,
    ReviewFinding,
    ReviewReport,
    TriageDecision,
    Usage,
)


class LLMError(RuntimeError):
    pass


ENGINEERING_DISCIPLINE = """
Engineering rules:
- Plan before writing: return a short, dependency-ordered plan.
- Reuse existing project patterns and helpers before adding anything new.
- Keep the diff small and limited to the requested issue.
- Ship tests with behavior changes, and include test, lint, or type commands.
- Keep every source file at or below 400 lines and avoid oversized functions.
- Avoid filler comments, dead code, speculative abstractions, and unrelated cleanup.
"""


class MockLLM:
    def triage(self, issue: Issue, context: list[ContextFile]) -> TriageDecision:
        body = f"{issue.title}\n{issue.body}".lower()
        if "clarify" in body or "unspecified" in body or "?" in body:
            return TriageDecision(
                ready=False,
                questions=["What exact behavior should the implementation use?"],
                reason="The issue explicitly signals an unresolved choice.",
            )
        return TriageDecision(
            ready=True,
            questions=[],
            reason="Mock triage marks this issue ready.",
        )

    def implement(
        self,
        issue: Issue,
        context: list[ContextFile],
        review_findings: list[str] | None = None,
    ) -> ImplementationPlan:
        line = f"- Autobot touched issue #{issue.number}: {issue.title}\n"
        readme = next((item for item in context if item.path.lower() == "readme.md"), None)
        content = (readme.content if readme else "# Demo\n") + "\n" + line
        return ImplementationPlan(
            plan=["Append a visible issue marker to README.md for the prototype dry run."],
            changes=[FileChange(path="README.md", content=content)],
            test_commands=["python -m unittest discover -s tests || true"],
        )

    def write_tests(self, issue: Issue, context: list[ContextFile]) -> ImplementationPlan:
        content = (
            "from pathlib import Path\n\n\n"
            "def test_issue_marker_present():\n"
            "    readme = Path('README.md').read_text(encoding='utf-8')\n"
            f"    assert 'Autobot touched issue #{issue.number}' in readme\n"
        )
        return ImplementationPlan(
            plan=["Add a smoke acceptance test for the issue marker."],
            changes=[FileChange(path=f"tests/test_issue_{issue.number}.py", content=content)],
            test_commands=["python -m pytest"],
        )

    def review(
        self,
        lens: str,
        issue: Issue,
        diff: str,
        model: str | None = None,
    ) -> ReviewReport:
        usage = Usage("review", model or "mock-review", 0, 0, 0)
        return ReviewReport(lens=lens, findings=[], usage=usage)


class HttpLLM:
    def __init__(self, config: Config) -> None:
        self.config = config
        provider = config.llm_provider
        if provider is None:
            provider = "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "openai"
        self.provider = provider

    def triage(self, issue: Issue, context: list[ContextFile]) -> TriageDecision:
        prompt = (
            "Decide if this issue is specified enough to implement without guessing. "
            "Ask only when a wrong guess is likely. Return JSON with keys: "
            "ready boolean, questions array of strings, reason string.\n\n"
            f"Issue: {issue.title}\n\n{issue.body}\n\nComments:\n{_comments(issue)}\n\n"
            f"Repo context:\n{format_context(context)}"
        )
        data, usage = self._json_call("triage", self.config.triage_model, prompt)
        from autobot.schemas import TriagePayload

        payload = TriagePayload.model_validate(data)
        return TriageDecision(
            ready=payload.ready,
            questions=payload.questions,
            reason=payload.reason,
            usage=usage,
        )

    def implement(
        self,
        issue: Issue,
        context: list[ContextFile],
        review_findings: list[str] | None = None,
    ) -> ImplementationPlan:
        findings = "\n".join(review_findings or [])
        prompt = (
            "You are implementing a GitHub issue in a checked-out repository. "
            "Return strict JSON with keys: plan array of strings, changes array, "
            "test_commands array. Each change has path, action write/delete, and content. "
            "For writes, provide complete file content, not patches. Keep changes small.\n"
            f"{ENGINEERING_DISCIPLINE}\n"
            f"Issue: {issue.title}\n\n{issue.body}\n\nComments:\n{_comments(issue)}\n\n"
            f"Blocking review findings to fix:\n{findings or 'None'}\n\n"
            f"Repo context:\n{format_context(context)}"
        )
        data, usage = self._json_call("implement", self.config.implement_model, prompt)
        from autobot.schemas import ImplementationPayload

        payload = ImplementationPayload.model_validate(data)
        changes = [
            FileChange(
                path=item.path,
                action=item.action,
                content=item.content,
            )
            for item in payload.changes
        ]
        return ImplementationPlan(
            plan=payload.plan,
            changes=changes,
            test_commands=payload.test_commands,
            usage=usage,
        )

    def write_tests(self, issue: Issue, context: list[ContextFile]) -> ImplementationPlan:
        prompt = (
            "You are the test author for a GitHub issue. Write acceptance tests derived "
            "only from the issue, comments, and repo conventions. Return strict JSON with "
            "keys: plan array of strings, changes array, test_commands array. Each change "
            "has path, action write/delete, and content. For writes, provide complete file "
            "content, not patches. Do not implement product code.\n"
            f"{ENGINEERING_DISCIPLINE}\n"
            f"Issue: {issue.title}\n\n{issue.body}\n\nComments:\n{_comments(issue)}\n\n"
            f"Repo context:\n{format_context(context)}"
        )
        data, usage = self._json_call("test", self.config.implement_model, prompt)
        from autobot.schemas import ImplementationPayload

        payload = ImplementationPayload.model_validate(data)
        changes = [
            FileChange(path=item.path, action=item.action, content=item.content)
            for item in payload.changes
        ]
        return ImplementationPlan(
            plan=payload.plan,
            changes=changes,
            test_commands=payload.test_commands,
            usage=usage,
        )

    def review(
        self,
        lens: str,
        issue: Issue,
        diff: str,
        model: str | None = None,
    ) -> ReviewReport:
        model = model or self.config.review_model
        prompt = (
            f"Review this diff with the lens: {lens}. Return strict JSON with key findings. "
            "Each finding has severity, file, line, message, blocking boolean. "
            "Blocking means the PR should not be opened until fixed.\n\n"
            f"Issue: {issue.title}\n\n{issue.body}\n\nDiff:\n{diff[:30000]}"
        )
        data, usage = self._json_call("review", model, prompt)
        from autobot.schemas import ReviewPayload

        payload = ReviewPayload.model_validate(data)
        findings = [
            ReviewFinding(
                severity=item.severity,
                file=item.file,
                line=item.line,
                message=item.message,
                blocking=item.blocking,
            )
            for item in payload.findings
        ]
        return ReviewReport(lens=lens, findings=findings, usage=usage)

    def _json_call(self, role: str, model: str, prompt: str) -> tuple[dict[str, Any], Usage]:
        if self.provider == "anthropic":
            return self._anthropic_json(role, model, prompt)
        if self.provider == "openai":
            return self._openai_json(role, model, prompt)
        raise LLMError(f"unknown LLM_PROVIDER: {self.provider}")

    def _openai_json(self, role: str, model: str, prompt: str) -> tuple[dict[str, Any], Usage]:
        token = os.getenv("OPENAI_API_KEY")
        if not token:
            raise LLMError("OPENAI_API_KEY is required unless AUTOBOT_MOCK_LLM=1")
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        data = _post_json("https://api.openai.com/v1/chat/completions", token, body)
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return _parse_json(content), Usage(
            role=role,
            model=model,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            dollars=_priced(role, usage.get("prompt_tokens"), usage.get("completion_tokens")),
        )

    def _anthropic_json(self, role: str, model: str, prompt: str) -> tuple[dict[str, Any], Usage]:
        token = os.getenv("ANTHROPIC_API_KEY")
        if not token:
            raise LLMError("ANTHROPIC_API_KEY is required unless AUTOBOT_MOCK_LLM=1")
        request = urllib.request.Request("https://api.anthropic.com/v1/messages", method="POST")
        request.add_header("x-api-key", token)
        request.add_header("anthropic-version", "2023-06-01")
        request.add_header("content-type", "application/json")
        body = json.dumps(
            {
                "model": model,
                "max_tokens": 4096,
                "system": "Return only valid JSON.",
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")
        with urllib.request.urlopen(request, data=body, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = "".join(part.get("text", "") for part in data.get("content", []))
        usage = data.get("usage", {})
        return _parse_json(content), Usage(
            role=role,
            model=model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            dollars=_priced(role, usage.get("input_tokens"), usage.get("output_tokens")),
        )


def build_llm(config: Config):
    if config.mock_llm or config.dry_run:
        return MockLLM()
    return HttpLLM(config)


def _post_json(url: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise LLMError(f"model did not return JSON: {text[:200]}") from exc
        return json.loads(match.group(0))


def _comments(issue: Issue) -> str:
    if not issue.comments:
        return "None"
    return "\n\n".join(f"{comment.author}: {comment.body}" for comment in issue.comments[-12:])


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _priced(role: str, input_tokens: Any, output_tokens: Any) -> float | None:
    price_role = "IMPLEMENT" if role == "test" else role.upper()
    in_price = os.getenv(f"{role.upper()}_INPUT_PRICE_PER_1K") or os.getenv(
        f"{price_role}_INPUT_PRICE_PER_1K"
    )
    out_price = os.getenv(f"{role.upper()}_OUTPUT_PRICE_PER_1K") or os.getenv(
        f"{price_role}_OUTPUT_PRICE_PER_1K"
    )
    if not in_price or not out_price:
        return None
    total = (int(input_tokens or 0) / 1000) * float(in_price)
    total += (int(output_tokens or 0) / 1000) * float(out_price)
    return round(total, 6)
