from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from autobot.models import IssueRecord, IssueState, TriageDecision, utc_now


class WorkflowPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class WorkflowStep(StrEnum):
    LOAD_RECORD = "load_record"
    TERMINAL_CHECK = "terminal_check"
    BUDGET_GATE = "budget_gate"
    READ_ISSUE = "read_issue"
    RESUME_WAITING = "resume_waiting"
    GUARDRAIL = "guardrail"
    PREPARE_WORKSPACE = "prepare_workspace"
    TRIAGE = "triage"
    IMPLEMENT_REVIEW_FINALIZE = "implement_review_finalize"
    FINISH = "finish"


class PauseKind(StrEnum):
    CLARIFICATION = "clarification"
    COMMENT_LIMIT = "comment_limit"
    BUDGET = "budget"
    OUT_OF_SCOPE = "out_of_scope"


class ContinueStep(WorkflowPayload):
    kind: Literal["continue"] = "continue"
    next_step: WorkflowStep


class WaitStep(WorkflowPayload):
    kind: Literal["wait"] = "wait"
    pause: PauseKind
    message: str


class TerminalStep(WorkflowPayload):
    kind: Literal["terminal"] = "terminal"
    mode: Literal["finish", "stored"] = "finish"
    message: str
    pr_url: str | None = None


class AbortStep(WorkflowPayload):
    kind: Literal["abort"] = "abort"
    message: str


type StepResult = ContinueStep | WaitStep | TerminalStep | AbortStep
_STEP_RESULT_ADAPTER: TypeAdapter[StepResult] = TypeAdapter(
    Annotated[StepResult, Field(discriminator="kind")]
)

_ALLOWED_TRANSITIONS: dict[WorkflowStep, frozenset[WorkflowStep]] = {
    WorkflowStep.LOAD_RECORD: frozenset({WorkflowStep.TERMINAL_CHECK}),
    WorkflowStep.TERMINAL_CHECK: frozenset({WorkflowStep.BUDGET_GATE}),
    WorkflowStep.BUDGET_GATE: frozenset({WorkflowStep.READ_ISSUE}),
    WorkflowStep.READ_ISSUE: frozenset({WorkflowStep.RESUME_WAITING}),
    WorkflowStep.RESUME_WAITING: frozenset({WorkflowStep.GUARDRAIL}),
    WorkflowStep.GUARDRAIL: frozenset({WorkflowStep.PREPARE_WORKSPACE}),
    WorkflowStep.PREPARE_WORKSPACE: frozenset({WorkflowStep.TRIAGE}),
    WorkflowStep.TRIAGE: frozenset({WorkflowStep.IMPLEMENT_REVIEW_FINALIZE}),
    WorkflowStep.IMPLEMENT_REVIEW_FINALIZE: frozenset({WorkflowStep.FINISH}),
    WorkflowStep.FINISH: frozenset(),
}


class HumanReply(WorkflowPayload):
    id: int
    author: str
    body: str
    created_at: str


class TriageRecord(WorkflowPayload):
    ready: bool
    questions: list[str]
    reason: str
    at: str


class GuardrailPause(WorkflowPayload):
    topics: list[str] | None = None
    question: str | None = None
    comment_id: int | None = None
    at: str | None = None


class CommentLimitPause(WorkflowPayload):
    kind: Literal["guardrail", "clarification"]
    questions: list[str]
    topics: list[str]
    at: str


class BudgetPause(WorkflowPayload):
    phase: str
    at: str
    cost: dict[str, Any]
    comment_id: int | None = None
    comment_skipped: Literal["comment_limit"] | None = None


class ClarificationAfterResume(WorkflowPayload):
    questions: list[str]
    reason: str
    at: str


class WorkflowConversation(WorkflowPayload):
    triage: TriageRecord | None = None
    asked_comment_id: int | None = None
    asked_at: str | None = None
    asked_questions: list[str] | None = None
    resume_after_comment_id: int | None = None
    human_replies: list[HumanReply] | None = None
    guardrail_pause: GuardrailPause | None = None
    comment_limit_pause: CommentLimitPause | None = None
    budget_pause: BudgetPause | None = None
    budget_resumed_at: str | None = None
    clarification_after_resume: ClarificationAfterResume | None = None
    review_reports: list[dict[str, Any]] | None = None
    ci_status: dict[str, Any] | None = None
    pr_url: str | None = None
    label_warnings: list[dict[str, Any]] | None = None
    audit_warnings: list[dict[str, Any]] | None = None
    finalize_warnings: list[dict[str, Any]] | None = None

    @classmethod
    def from_record(cls, record: IssueRecord) -> WorkflowConversation:
        return cls.model_validate(record.conversation)

    def save(self, record: IssueRecord) -> None:
        record.conversation = self.model_dump(mode="json", exclude_none=True)

    def resume_marker(self) -> int:
        ids = [int(self.resume_after_comment_id or 0), int(self.asked_comment_id or 0)]
        for pause in (self.guardrail_pause, self.budget_pause):
            if pause is not None:
                ids.append(int(pause.comment_id or 0))
        return max(ids)

    def record_triage(self, decision: TriageDecision) -> None:
        self.triage = TriageRecord(
            ready=decision.ready,
            questions=decision.questions,
            reason=decision.reason,
            at=utc_now(),
        )

    def record_clarification_pause(self, comment_id: int, questions: list[str]) -> None:
        self.asked_comment_id = comment_id
        self.asked_at = utc_now()
        self.asked_questions = questions
        self.resume_after_comment_id = comment_id

    def record_guardrail_pause(
        self,
        topics: list[str],
        question: str,
        comment_id: int,
    ) -> None:
        self.guardrail_pause = GuardrailPause(
            topics=topics,
            question=question,
            comment_id=comment_id,
            at=utc_now(),
        )
        self.resume_after_comment_id = comment_id

    def record_comment_limit_pause(
        self,
        kind: Literal["guardrail", "clarification"],
        questions: list[str],
        resume_after_comment_id: int,
        topics: list[str] | None = None,
    ) -> None:
        self.comment_limit_pause = CommentLimitPause(
            kind=kind,
            questions=questions,
            topics=topics or [],
            at=utc_now(),
        )
        self.resume_after_comment_id = resume_after_comment_id

    def clear_comment_limit_pause(self) -> None:
        self.comment_limit_pause = None

    def pop_comment_limit_pause(self) -> CommentLimitPause | None:
        pause = self.comment_limit_pause
        self.comment_limit_pause = None
        return pause

    def record_budget_pause(self, phase: str, cost: dict[str, Any]) -> BudgetPause:
        self.budget_pause = BudgetPause(
            phase=phase,
            at=utc_now(),
            cost=cost,
        )
        self.resume_after_comment_id = 0
        return self.budget_pause

    def record_budget_comment(self, comment_id: int) -> None:
        if self.budget_pause is None:
            return
        self.budget_pause.comment_id = comment_id
        self.resume_after_comment_id = comment_id

    def record_budget_comment_skipped(self) -> None:
        if self.budget_pause is not None:
            self.budget_pause.comment_skipped = "comment_limit"

    def record_budget_resumed(self) -> None:
        self.budget_resumed_at = utc_now()

    def record_human_replies(self, new_replies: list[HumanReply]) -> None:
        replies = list(self.human_replies or [])
        seen_ids = {reply.id for reply in replies}
        for reply in new_replies:
            if reply.id in seen_ids:
                continue
            replies.append(reply)
            seen_ids.add(reply.id)
        self.human_replies = replies

    def record_clarification_still_needed(self, questions: list[str], reason: str) -> None:
        self.clarification_after_resume = ClarificationAfterResume(
            questions=questions,
            reason=reason,
            at=utc_now(),
        )
        reply_ids = [reply.id for reply in self.human_replies or []]
        if reply_ids:
            self.resume_after_comment_id = max(reply_ids)

    def record_review_round(self, artifact: dict[str, Any]) -> None:
        reports = list(self.review_reports or [])
        reports.append(artifact)
        self.review_reports = reports

    def record_pr_open(self, pr_url: str, ci_status: dict[str, Any]) -> None:
        self.ci_status = ci_status
        self.pr_url = pr_url


def validate_step_result(value: object) -> StepResult:
    return _STEP_RESULT_ADAPTER.validate_python(value)


def validate_workflow_transition(current: WorkflowStep, next_step: WorkflowStep) -> None:
    if next_step not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid workflow transition: {current.value} -> {next_step.value}")


def pause_from_blocked(blocked_on: str | None) -> PauseKind:
    if blocked_on == "budget":
        return PauseKind.BUDGET
    if blocked_on == "comment_limit":
        return PauseKind.COMMENT_LIMIT
    if blocked_on == "out_of_scope":
        return PauseKind.OUT_OF_SCOPE
    return PauseKind.CLARIFICATION


def validate_pr_open_evidence(record: IssueRecord) -> None:
    conversation = WorkflowConversation.from_record(record)
    if record.state != IssueState.PR_OPEN:
        return
    if not (record.pr_url or conversation.pr_url):
        raise ValueError("pr_open requires a stored draft PR URL")
    if record.review_rounds < 1:
        raise ValueError("pr_open requires at least one review round")
    if not record.files_touched:
        raise ValueError("pr_open requires touched-file evidence")
