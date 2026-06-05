from __future__ import annotations

import time

from autobot.cost import CostLedger
from autobot.models import IssueRecord, ProcessResult
from autobot.state import StateStore


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
        blocked_on=record.blocked_on,
    )
