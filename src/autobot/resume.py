from __future__ import annotations

from autobot.models import IssueRecord


def resume_after_comment_id(record: IssueRecord) -> int:
    ids = [int(record.conversation.get("resume_after_comment_id") or 0)]
    ids.append(int(record.conversation.get("asked_comment_id") or 0))
    for key in ("guardrail_pause", "budget_pause"):
        pause = record.conversation.get(key) or {}
        ids.append(int(pause.get("comment_id") or 0))
    return max(ids)
