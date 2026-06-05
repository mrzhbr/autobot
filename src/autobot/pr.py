from __future__ import annotations

import json
from datetime import datetime

from autobot.cost import CostLedger
from autobot.models import Issue, IssueRecord


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
        f"```text\n{test_output[-8000:]}\n```\n</details>\n\n"
    )
    return (
        f"Implements #{issue.number}.\n\n"
        "## Summary\n"
        + "\n".join(f"- {item}" for item in record.plan.get("plan", []))
        + "\n\n## Assumptions / clarifications\n"
        f"```json\n{assumptions_json}\n```\n\n"
        "## Verification\n"
        f"- Acceptance test baseline: {baseline_state}\n"
        + "\n".join(f"- `{command}`" for command in verification_commands)
        + test_details
        + "## Cost\n"
        f"- Input tokens: {ledger.input_tokens}\n"
        f"- Output tokens: {ledger.output_tokens}\n"
        f"- Dollars: {ledger.dollars if ledger.dollars is not None else 'not configured'}\n"
        f"- Wall seconds: {_wall_seconds(ledger.started_at)}\n"
        f"- Review rounds: {record.review_rounds}\n"
        f"- CI status: {ci_status.get('state', 'unknown')}\n"
    )


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
