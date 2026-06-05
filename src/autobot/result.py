from __future__ import annotations

import time

from autobot.cost import CostLedger
from autobot.models import IssueRecord, IssueState, ProcessResult
from autobot.scanner import redact_secret_like_values
from autobot.state import StateStore


def abandon_process(
    store: StateStore,
    record: IssueRecord,
    ledger: CostLedger,
    exc: Exception,
    started: float,
) -> str:
    message = redact_secret_like_values(str(exc))
    record.transition(IssueState.ABANDONED)
    record.blocked_on = message
    finish_process(store, record, ledger, message, None, started)
    return message


def terminal_process_result(record: IssueRecord, message: str) -> ProcessResult:
    return ProcessResult(
        state=record.state,
        message=message,
        pr_url=record.pr_url or record.conversation.get("pr_url"),
        cost=record.cost,
        branch=record.branch,
        review_rounds=record.review_rounds,
        files_touched=record.files_touched,
        verification_commands=list(record.plan.get("verification_commands") or []),
        blocked_on=record.blocked_on,
    )


def finish_process(
    store: StateStore,
    record: IssueRecord,
    ledger: CostLedger,
    message: str,
    pr_url: str | None,
    started: float,
) -> ProcessResult:
    ledger.finish()
    cost = ledger.to_dict()
    cost["wall_seconds"] = round(time.monotonic() - started, 2)
    record.cost = cost
    if pr_url:
        record.pr_url = pr_url
    store.upsert(record)
    resolved_pr_url = pr_url or record.pr_url or record.conversation.get("pr_url")
    return ProcessResult(
        state=record.state,
        message=message,
        pr_url=resolved_pr_url,
        cost=cost,
        branch=record.branch,
        review_rounds=record.review_rounds,
        files_touched=record.files_touched,
        verification_commands=list(record.plan.get("verification_commands") or []),
        blocked_on=record.blocked_on,
    )
