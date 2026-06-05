from __future__ import annotations

from autobot.cost import CostLedger
from autobot.models import Issue, IssueRecord, IssueState, utc_now
from autobot.scanner import redact_secret_like_values


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
    new_replies = [
        {
            "id": comment.id,
            "author": comment.author,
            "body": redact_secret_like_values(comment.body),
            "created_at": comment.created_at,
        }
        for comment in issue.comments
        if comment.id > resume_after and not _same_login(comment.author, bot)
    ]
    if not new_replies:
        return False
    record.conversation["human_replies"] = _append_human_replies(record, new_replies)
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
    replies = record.conversation.get("human_replies") or []
    reply_ids = [int(reply.get("id") or 0) for reply in replies if isinstance(reply, dict)]
    if reply_ids:
        record.conversation["resume_after_comment_id"] = max(reply_ids)
    record.blocked_on = "clarification"
    record.transition(IssueState.WAITING)


def _append_human_replies(
    record: IssueRecord,
    new_replies: list[dict],
) -> list[dict]:
    replies = [
        reply for reply in record.conversation.get("human_replies", []) if isinstance(reply, dict)
    ]
    seen_ids = {int(reply.get("id") or 0) for reply in replies}
    for reply in new_replies:
        reply_id = int(reply.get("id") or 0)
        if reply_id in seen_ids:
            continue
        replies.append(reply)
        seen_ids.add(reply_id)
    return replies


def _same_login(author: str, bot: str | None) -> bool:
    return bool(bot) and author.casefold() == bot.casefold()
