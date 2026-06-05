from __future__ import annotations

import json
from datetime import datetime

from autobot.cost import CostLedger
from autobot.models import Issue, IssueRecord
from autobot.scanner import redact_secret_like_values


def build_pr_body(
    issue: Issue,
    record: IssueRecord,
    ledger: CostLedger,
    verification_commands: list[str],
    test_output: str,
    ci_status: dict,
) -> str:
    assumptions = record.conversation.get("human_replies") or record.conversation.get(
        "triage",
        {},
    )
    baseline = record.plan.get("acceptance_test_baseline") or {}
    baseline_state = _baseline_state(baseline)
    assumptions_json = json.dumps(assumptions, indent=2, sort_keys=True)
    test_details = (
        "\n\n<details><summary>Test output</summary>\n\n"
        + _fenced_block("text", test_output[-8000:])
        + "\n</details>\n\n"
    )
    body = (
        f"Implements #{issue.number}.\n\n"
        "## Summary\n"
        + "\n".join(f"- {item}" for item in record.plan.get("plan", []))
        + "\n\n## Assumptions / clarifications\n"
        + _fenced_block("json", assumptions_json)
        + "\n\n"
        "## Verification\n"
        f"- Acceptance test baseline: {baseline_state}\n"
        + "\n".join(f"- {_inline_code(command)}" for command in verification_commands)
        + test_details
        + "## Cost\n"
        f"- Input tokens: {ledger.input_tokens}\n"
        f"- Output tokens: {ledger.output_tokens}\n"
        f"- Dollars: {ledger.dollars if ledger.dollars is not None else 'not configured'}\n"
        f"- Wall seconds: {_wall_seconds(ledger.started_at)}\n"
        f"- Review rounds: {record.review_rounds}\n"
        f"- CI status: {ci_status.get('state', 'unknown')}\n"
    )
    return redact_secret_like_values(body)


def _fenced_block(language: str, content: str) -> str:
    fence = "```"
    while fence in content:
        fence += "`"
    return f"{fence}{language}\n{content}\n{fence}"


def _inline_code(content: str) -> str:
    fence = "`"
    while fence in content:
        fence += "`"
    padding = " " if content.startswith("`") or content.endswith("`") else ""
    return f"{fence}{padding}{content}{padding}{fence}"


def _wall_seconds(started_at: str) -> str:
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return "not recorded"
    return str(round((datetime.now(started.tzinfo) - started).total_seconds(), 2))


def _baseline_state(baseline: dict) -> str:
    if "ok" not in baseline:
        return "not recorded"
    return "pass" if baseline.get("ok") else "failed"
