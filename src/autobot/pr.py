from __future__ import annotations

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
    test_details = (
        "\n\n<details><summary>Test output</summary>\n\n"
        f"```text\n{test_output[-8000:]}\n```\n</details>\n\n"
    )
    return (
        f"Implements #{issue.number}.\n\n"
        "## Summary\n"
        + "\n".join(f"- {item}" for item in record.plan.get("plan", []))
        + "\n\n## Assumptions / clarifications\n"
        f"```json\n{assumptions}\n```\n\n"
        "## Verification\n"
        + "\n".join(f"- `{command}`" for command in verification_commands)
        + test_details
        + "## Cost\n"
        f"- Input tokens: {ledger.input_tokens}\n"
        f"- Output tokens: {ledger.output_tokens}\n"
        f"- Dollars: {ledger.dollars if ledger.dollars is not None else 'not configured'}\n"
        f"- Review rounds: {record.review_rounds}\n"
        f"- CI status: {ci_status.get('state', 'unknown')}\n"
    )
