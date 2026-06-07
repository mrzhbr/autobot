from __future__ import annotations

from autobot.cost import CostLedger
from autobot.models import Issue, IssueRecord, IssueState
from autobot.scanner import redact_secret_like_values
from autobot.workflow_models import HumanReply, WorkflowConversation


class PausedForHuman(RuntimeError):
    pass


def resume_after_comment_id(record: IssueRecord) -> int:
    return WorkflowConversation.from_record(record).resume_marker()


def latest_comment_id(issue: Issue) -> int:
    return max((comment.id for comment in issue.comments), default=0)


def resume_if_answered(record: IssueRecord, issue: Issue, bot: str | None) -> bool:
    resume_after = resume_after_comment_id(record)
    new_replies = [
        HumanReply(
            id=comment.id,
            author=comment.author,
            body=redact_secret_like_values(comment.body),
            created_at=comment.created_at,
        )
        for comment in issue.comments
        if comment.id > resume_after and not _same_login(comment.author, bot)
    ]
    if not new_replies:
        return False
    conversation = WorkflowConversation.from_record(record)
    conversation.record_human_replies(new_replies)
    conversation.save(record)
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
    conversation = WorkflowConversation.from_record(record)
    conversation.record_budget_resumed()
    conversation.save(record)
    record.transition(IssueState.RESUMED)
    record.blocked_on = None
    return True


def mark_clarification_still_needed(
    record: IssueRecord,
    questions: list[str],
    reason: str,
) -> None:
    conversation = WorkflowConversation.from_record(record)
    conversation.record_clarification_still_needed(questions, reason)
    conversation.save(record)
    record.blocked_on = "clarification"
    record.transition(IssueState.WAITING)


def _same_login(author: str, bot: str | None) -> bool:
    return bool(bot) and author.casefold() == bot.casefold()
