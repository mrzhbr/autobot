from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import assert_never

from autobot import resume
from autobot.adapters import LLM, ChatChannel, GitHost, IssueTracker
from autobot.audit import AuditLog
from autobot.config import Config
from autobot.context import gather_context
from autobot.cost import CostLedger
from autobot.guardrails import detect_out_of_scope
from autobot.implementation import ImplementationRunner
from autobot.models import ContextFile, Issue, IssueRecord, IssueState, ProcessResult
from autobot.result import abandon_process, finish_process, terminal_process_result
from autobot.state import StateStore
from autobot.workflow_models import (
    AbortStep,
    ContinueStep,
    PauseKind,
    StepResult,
    TerminalStep,
    WaitStep,
    WorkflowConversation,
    WorkflowStep,
    pause_from_blocked,
    validate_pr_open_evidence,
    validate_step_result,
    validate_workflow_transition,
)
from autobot.workflow_pauses import WorkflowPauses
from autobot.workspace import branch_name, prepare_dry_run_repo, repo_work_dir


@dataclass
class WorkflowContext:
    repo: str
    issue_number: int
    started: float
    record: IssueRecord | None = None
    ledger: CostLedger | None = None
    issue: Issue | None = None
    repo_dir: Path | None = None
    context: list[ContextFile] = field(default_factory=list)
    resumed: bool = False
    previous_blocked_on: str | None = None
    pr_url: str | None = None

    def require_record(self) -> IssueRecord:
        if self.record is None:
            raise RuntimeError("workflow record is not loaded")
        return self.record

    def require_ledger(self) -> CostLedger:
        if self.ledger is None:
            raise RuntimeError("workflow ledger is not loaded")
        return self.ledger

    def require_issue(self) -> Issue:
        if self.issue is None:
            raise RuntimeError("workflow issue is not loaded")
        return self.issue

    def require_repo_dir(self) -> Path:
        if self.repo_dir is None:
            raise RuntimeError("workflow repo directory is not prepared")
        return self.repo_dir


class IssueWorkflow(WorkflowPauses):
    def __init__(
        self,
        config: Config,
        store: StateStore,
        tracker: IssueTracker,
        git_host: GitHost,
        chat: ChatChannel,
        llm: LLM,
        audit: AuditLog,
        progress: Callable[[WorkflowStep], None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.tracker = tracker
        self.git_host = git_host
        self.chat = chat
        self.llm = llm
        self.audit = audit
        self.progress = progress
        self.comments_this_run = 0

    def process(self, repo: str, issue_number: int) -> ProcessResult:
        self.comments_this_run = 0
        ctx = WorkflowContext(repo=repo, issue_number=issue_number, started=time.monotonic())
        step = WorkflowStep.LOAD_RECORD
        while True:
            try:
                if self.progress is not None:
                    self.progress(step)
                result = validate_step_result(self._run_step(step, ctx))
            except resume.PausedForHuman as exc:
                return self._finish_wait(ctx, str(exc))
            except Exception as exc:
                if step in {WorkflowStep.READ_ISSUE, WorkflowStep.RESUME_WAITING}:
                    raise
                return self._abandon(ctx, exc)

            if isinstance(result, ContinueStep):
                validate_workflow_transition(step, result.next_step)
                step = result.next_step
                continue
            if isinstance(result, WaitStep):
                return self._finish_wait(ctx, result.message)
            if isinstance(result, TerminalStep):
                return self._finish_terminal(ctx, result)
            if isinstance(result, AbortStep):
                return self._abandon(ctx, RuntimeError(result.message))
            assert_never(result)

    def _run_step(self, step: WorkflowStep, ctx: WorkflowContext) -> StepResult:
        if step == WorkflowStep.LOAD_RECORD:
            return self._load_record(ctx)
        if step == WorkflowStep.TERMINAL_CHECK:
            return self._terminal_check(ctx)
        if step == WorkflowStep.BUDGET_GATE:
            return self._budget_gate(ctx)
        if step == WorkflowStep.READ_ISSUE:
            return self._read_issue(ctx)
        if step == WorkflowStep.RESUME_WAITING:
            return self._resume_waiting(ctx)
        if step == WorkflowStep.GUARDRAIL:
            return self._guardrail(ctx)
        if step == WorkflowStep.PREPARE_WORKSPACE:
            return self._prepare_workspace(ctx)
        if step == WorkflowStep.TRIAGE:
            return self._triage(ctx)
        if step == WorkflowStep.IMPLEMENT_REVIEW_FINALIZE:
            return self._implement_review_and_pr(ctx)
        if step == WorkflowStep.FINISH:
            return self._finish(ctx)
        assert_never(step)

    def _load_record(self, ctx: WorkflowContext) -> ContinueStep:
        record = self.store.ensure(ctx.repo, ctx.issue_number)
        ctx.record = record
        ctx.ledger = CostLedger(record.cost)
        return ContinueStep(next_step=WorkflowStep.TERMINAL_CHECK)

    def _terminal_check(self, ctx: WorkflowContext) -> ContinueStep | TerminalStep:
        record = ctx.require_record()
        if record.state == IssueState.ABANDONED:
            return TerminalStep(
                mode="stored",
                message="issue is abandoned; clear the state record before retrying",
            )
        if record.state == IssueState.PR_OPEN:
            return TerminalStep(mode="stored", message="draft pull request already open")
        return ContinueStep(next_step=WorkflowStep.BUDGET_GATE)

    def _budget_gate(self, ctx: WorkflowContext) -> ContinueStep | WaitStep:
        record = ctx.require_record()
        ledger = ctx.require_ledger()
        if not ledger.hit_budget(self.config.max_issue_tokens, self.config.max_issue_dollars):
            return ContinueStep(next_step=WorkflowStep.READ_ISSUE)
        if record.blocked_on == "budget":
            return WaitStep(pause=PauseKind.BUDGET, message="waiting for budget increase")
        issue_ref = Issue(ctx.repo, ctx.issue_number, "", "", "unknown", [])
        self._pause_if_budget_hit(issue_ref, record, ledger, "run start")
        return WaitStep(pause=PauseKind.BUDGET, message="waiting for budget increase")

    def _read_issue(self, ctx: WorkflowContext) -> ContinueStep:
        ctx.issue = self.tracker.get(ctx.repo, ctx.issue_number)
        return ContinueStep(next_step=WorkflowStep.RESUME_WAITING)

    def _resume_waiting(self, ctx: WorkflowContext) -> ContinueStep | WaitStep:
        record = ctx.require_record()
        ledger = ctx.require_ledger()
        issue = ctx.require_issue()
        ctx.previous_blocked_on = record.blocked_on
        if record.state != IssueState.WAITING:
            return ContinueStep(next_step=WorkflowStep.GUARDRAIL)

        if record.blocked_on == "comment_limit":
            result = self._resume_comment_limit_pause(issue, record)
            if result is not None:
                return result
            ctx.resumed = True
            return ContinueStep(next_step=WorkflowStep.GUARDRAIL)

        resumed, waiting_message = resume.resume_waiting(
            record,
            issue,
            self.config.agent_login,
            ledger,
            self.config.max_issue_tokens,
            self.config.max_issue_dollars,
        )
        if not resumed:
            return WaitStep(pause=pause_from_blocked(record.blocked_on), message=waiting_message)
        ctx.resumed = True
        self.store.upsert(record)
        return ContinueStep(next_step=WorkflowStep.GUARDRAIL)

    def _guardrail(self, ctx: WorkflowContext) -> ContinueStep | WaitStep:
        record = ctx.require_record()
        issue = ctx.require_issue()
        conversation = WorkflowConversation.from_record(record)
        replies = [reply.model_dump(mode="json") for reply in conversation.human_replies or []]
        topics = detect_out_of_scope(issue, replies)
        if ctx.resumed and topics and ctx.previous_blocked_on == "out_of_scope":
            record.blocked_on = "out_of_scope"
            record.transition(IssueState.WAITING)
            self.store.upsert(record)
            return WaitStep(
                pause=PauseKind.OUT_OF_SCOPE,
                message="still waiting on out-of-scope guardrail",
            )
        if topics and conversation.guardrail_pause is None:
            return self._pause_for_guardrail(issue, record, topics)
        return ContinueStep(next_step=WorkflowStep.PREPARE_WORKSPACE)

    def _prepare_workspace(self, ctx: WorkflowContext) -> ContinueStep:
        issue = ctx.require_issue()
        record = ctx.require_record()
        ctx.repo_dir = self._clone_and_branch(issue, record)
        ctx.context = gather_context(ctx.repo_dir, issue)
        return ContinueStep(next_step=WorkflowStep.TRIAGE)

    def _triage(self, ctx: WorkflowContext) -> ContinueStep | WaitStep:
        record = ctx.require_record()
        ledger = ctx.require_ledger()
        issue = ctx.require_issue()
        if record.state in {
            IssueState.SPEC_READY,
            IssueState.IMPLEMENTING,
            IssueState.REVIEW_LOOP,
            IssueState.PR_OPEN,
        }:
            return ContinueStep(next_step=WorkflowStep.IMPLEMENT_REVIEW_FINALIZE)

        triage = self.llm.triage(issue, ctx.context)
        ledger.add(triage.usage)
        conversation = WorkflowConversation.from_record(record)
        conversation.record_triage(triage)
        conversation.save(record)
        record.transition(IssueState.TRIAGED)
        self.store.upsert(record)
        self._pause_if_budget_hit(issue, record, ledger, "triage")
        if not triage.ready:
            if ctx.resumed and ctx.previous_blocked_on == "clarification":
                resume.mark_clarification_still_needed(record, triage.questions, triage.reason)
                self.store.upsert(record)
                return WaitStep(
                    pause=PauseKind.CLARIFICATION,
                    message="still needs clarification after reply",
                )
            return self._ask_and_wait(issue, record, triage.questions)
        record.transition(IssueState.SPEC_READY)
        self.store.upsert(record)
        return ContinueStep(next_step=WorkflowStep.IMPLEMENT_REVIEW_FINALIZE)

    def _implement_review_and_pr(self, ctx: WorkflowContext) -> ContinueStep:
        runner = ImplementationRunner(
            self.config,
            self.store,
            self.tracker,
            self.git_host,
            self.llm,
            self.audit,
            self._pause_if_budget_hit,
        )
        ctx.pr_url = runner.run(
            ctx.require_issue(),
            ctx.require_record(),
            ctx.require_ledger(),
            ctx.require_repo_dir(),
        )
        validate_pr_open_evidence(ctx.require_record())
        return ContinueStep(next_step=WorkflowStep.FINISH)

    def _finish(self, ctx: WorkflowContext) -> TerminalStep:
        return TerminalStep(
            mode="finish",
            message="opened draft pull request",
            pr_url=ctx.pr_url,
        )

    def _clone_and_branch(self, issue: Issue, record: IssueRecord) -> Path:
        repo_dir = repo_work_dir(self.config.work_root, issue)
        branch = record.branch or branch_name(issue)
        record.branch = branch
        if self.config.dry_run:
            prepare_dry_run_repo(repo_dir, branch)
            self.store.upsert(record)
            return repo_dir
        self.git_host.clone(issue.repo, repo_dir)
        self.git_host.create_branch(repo_dir, branch)
        self.store.upsert(record)
        return repo_dir

    def _finish_wait(self, ctx: WorkflowContext, message: str) -> ProcessResult:
        return finish_process(
            self.store,
            ctx.require_record(),
            ctx.require_ledger(),
            message,
            None,
            ctx.started,
        )

    def _finish_terminal(self, ctx: WorkflowContext, result: TerminalStep) -> ProcessResult:
        if result.mode == "stored":
            return terminal_process_result(ctx.require_record(), result.message)
        return finish_process(
            self.store,
            ctx.require_record(),
            ctx.require_ledger(),
            result.message,
            result.pr_url,
            ctx.started,
        )

    def _abandon(self, ctx: WorkflowContext, exc: Exception) -> ProcessResult:
        record = ctx.require_record()
        ledger = ctx.require_ledger()
        message = abandon_process(self.store, record, ledger, exc, ctx.started)
        raise RuntimeError(message) from exc
