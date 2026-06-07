from __future__ import annotations

from typing import Literal

from autobot import resume
from autobot.adapters import ChatChannel, IssueTracker
from autobot.audit import AuditLog, record_best_effort
from autobot.config import Config
from autobot.cost import CostLedger
from autobot.guardrails import guardrail_question
from autobot.labels import set_issue_label
from autobot.models import Issue, IssueRecord, IssueState
from autobot.state import StateStore
from autobot.workflow_models import PauseKind, WaitStep, WorkflowConversation


class WorkflowPauses:
    config: Config
    store: StateStore
    tracker: IssueTracker
    chat: ChatChannel
    audit: AuditLog
    comments_this_run: int

    def _pause_for_guardrail(
        self,
        issue: Issue,
        record: IssueRecord,
        topics: list[str],
    ) -> WaitStep:
        question = guardrail_question(topics)
        if self.comments_this_run >= self.config.comment_limit:
            return self._pause_for_comment_limit(issue, record, "guardrail", [question], topics)
        if self.config.dry_run:
            comment_id = resume.latest_comment_id(issue)
        else:
            comment_id = self.chat.ask(issue, [question])
            self.comments_this_run += 1
        record.transition(IssueState.ASKED)
        conversation = WorkflowConversation.from_record(record)
        conversation.record_guardrail_pause(topics, question, comment_id)
        conversation.save(record)
        record.blocked_on = "out_of_scope"
        self.store.upsert(record)
        record.transition(IssueState.WAITING)
        self.store.upsert(record)
        self._record_waiting_side_effects(issue, record, comment_id)
        return WaitStep(pause=PauseKind.OUT_OF_SCOPE, message="paused for out-of-scope guardrail")

    def _ask_and_wait(
        self,
        issue: Issue,
        record: IssueRecord,
        questions: list[str],
    ) -> WaitStep:
        record.transition(IssueState.NEEDS_SPEC)
        self.store.upsert(record)
        asked = questions[:3]
        if self.comments_this_run >= self.config.comment_limit:
            return self._pause_for_comment_limit(issue, record, "clarification", asked)
        if self.config.dry_run:
            comment_id = resume.latest_comment_id(issue)
        else:
            comment_id = self.chat.ask(issue, asked)
            self.comments_this_run += 1
        record.transition(IssueState.ASKED)
        conversation = WorkflowConversation.from_record(record)
        conversation.record_clarification_pause(comment_id, asked)
        conversation.save(record)
        self.store.upsert(record)
        record.blocked_on = "clarification"
        record.transition(IssueState.WAITING)
        self.store.upsert(record)
        self._record_waiting_side_effects(issue, record, comment_id)
        return WaitStep(
            pause=PauseKind.CLARIFICATION,
            message="posted clarification and entered waiting",
        )

    def _pause_for_comment_limit(
        self,
        issue: Issue,
        record: IssueRecord,
        kind: Literal["guardrail", "clarification"],
        questions: list[str],
        topics: list[str] | None = None,
    ) -> WaitStep:
        conversation = WorkflowConversation.from_record(record)
        conversation.record_comment_limit_pause(
            kind,
            questions,
            resume.latest_comment_id(issue),
            topics,
        )
        conversation.save(record)
        record.blocked_on = "comment_limit"
        record.transition(IssueState.WAITING)
        return WaitStep(
            pause=PauseKind.COMMENT_LIMIT,
            message="waiting for outbound comment capacity",
        )

    def _resume_comment_limit_pause(
        self,
        issue: Issue,
        record: IssueRecord,
    ) -> WaitStep | None:
        if resume.resume_if_answered(record, issue, self.config.agent_login):
            conversation = WorkflowConversation.from_record(record)
            conversation.clear_comment_limit_pause()
            conversation.save(record)
            self.store.upsert(record)
            return None
        if self.comments_this_run >= self.config.comment_limit:
            return WaitStep(
                pause=PauseKind.COMMENT_LIMIT,
                message="waiting for outbound comment capacity",
            )
        conversation = WorkflowConversation.from_record(record)
        pause = conversation.pop_comment_limit_pause()
        conversation.save(record)
        if pause is None:
            return WaitStep(
                pause=PauseKind.COMMENT_LIMIT,
                message="waiting for outbound comment capacity",
            )
        if pause.kind == "guardrail":
            return self._pause_for_guardrail(issue, record, pause.topics)
        return self._ask_and_wait(issue, record, pause.questions)

    def _pause_if_budget_hit(
        self,
        issue: Issue,
        record: IssueRecord,
        ledger: CostLedger,
        phase: str,
    ) -> None:
        if not ledger.hit_budget(self.config.max_issue_tokens, self.config.max_issue_dollars):
            return
        text = (
            "Autobot paused because the per-issue budget was reached during "
            f"{phase}. Increase `MAX_ISSUE_TOKENS` or `MAX_ISSUE_DOLLARS`, then rerun."
        )
        conversation = WorkflowConversation.from_record(record)
        conversation.record_budget_pause(phase, ledger.to_dict())
        record.blocked_on = "budget"
        comment_id: int | None = None
        if not self.config.dry_run:
            if self.comments_this_run >= self.config.comment_limit:
                conversation.record_budget_comment_skipped()
            else:
                comment_id = self.chat.notify(issue, text)
                self.comments_this_run += 1
                conversation.record_budget_comment(comment_id)
        conversation.save(record)
        record.transition(IssueState.WAITING)
        self.store.upsert(record)
        if not self.config.dry_run:
            if comment_id is not None:
                record_best_effort(
                    self.audit,
                    "comment",
                    issue.repo,
                    issue.number,
                    {"comment_id": comment_id},
                    record,
                )
            set_issue_label(self.tracker, self.audit, record, issue, "agent-waiting")
            self.store.upsert(record)
        raise resume.PausedForHuman(text)

    def _record_waiting_side_effects(
        self,
        issue: Issue,
        record: IssueRecord,
        comment_id: int,
    ) -> None:
        if self.config.dry_run:
            return
        record_best_effort(
            self.audit, "comment", issue.repo, issue.number, {"comment_id": comment_id}, record
        )
        set_issue_label(self.tracker, self.audit, record, issue, "agent-waiting")
        self.store.upsert(record)
