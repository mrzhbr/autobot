from __future__ import annotations

from autobot.models import IssueRecord, IssueState, utc_now


class PausedForHuman(RuntimeError):
    pass


def resume_after_comment_id(record: IssueRecord) -> int:
    ids = [int(record.conversation.get("resume_after_comment_id") or 0)]
    ids.append(int(record.conversation.get("asked_comment_id") or 0))
    for key in ("guardrail_pause", "budget_pause"):
        pause = record.conversation.get(key) or {}
        ids.append(int(pause.get("comment_id") or 0))
    return max(ids)


def mark_clarification_still_needed(
    record: IssueRecord,
    questions: list[str],
    reason: str,
) -> None:
    record.conversation["clarification_after_resume"] = {
        "questions": questions,
        "reason": reason,
        "at": utc_now(),
    }
    record.blocked_on = "clarification"
    record.transition(IssueState.WAITING)
