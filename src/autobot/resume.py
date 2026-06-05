from __future__ import annotations

from autobot.cost import CostLedger
from autobot.models import Issue, IssueRecord, IssueState, utc_now


class PausedForHuman(RuntimeError):
    pass


def resume_after_comment_id(record: IssueRecord) -> int:
    ids = [int(record.conversation.get("resume_after_comment_id") or 0)]
    ids.append(int(record.conversation.get("asked_comment_id") or 0))
    for key in ("guardrail_pause", "budget_pause"):
        pause = record.conversation.get(key) or {}
        ids.append(int(pause.get("comment_id") or 0))
    return max(ids)


def latest_comment_id(issue: Issue) -> int:
    return max((comment.id for comment in issue.comments), default=0)


def resume_if_answered(record: IssueRecord, issue: Issue, bot: str | None) -> bool:
    resume_after = resume_after_comment_id(record)
    replies = [
        {
            "id": comment.id,
            "author": comment.author,
            "body": comment.body,
            "created_at": comment.created_at,
        }
        for comment in issue.comments
        if comment.id > resume_after and comment.author != bot
    ]
    if not replies:
        return False
    record.conversation["human_replies"] = replies
    record.transition(IssueState.RESUMED)
    record.blocked_on = None
    return True


def resume_waiting(
    record: IssueRecord,
    issue: Issue,
    bot: str | None,
    ledger: CostLedger,
    max_tokens: int | None,
    max_dollars: float | None,
) -> tuple[bool, str]:
    if record.blocked_on == "budget":
        if resume_if_budget_allows(record, ledger, max_tokens, max_dollars):
            return True, ""
        return False, "waiting for budget increase"
    if resume_if_answered(record, issue, bot):
        return True, ""
    return False, "waiting for a human answer"


def resume_if_budget_allows(
    record: IssueRecord,
    ledger: CostLedger,
    max_tokens: int | None,
    max_dollars: float | None,
) -> bool:
    if record.blocked_on != "budget" or ledger.hit_budget(max_tokens, max_dollars):
        return False
    record.conversation["budget_resumed_at"] = utc_now()
    record.transition(IssueState.RESUMED)
    record.blocked_on = None
    return True


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
