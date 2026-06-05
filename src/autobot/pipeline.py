from __future__ import annotations

import time
from pathlib import Path

from autobot import resume
from autobot.adapters import LLM, ChatChannel, GitHost, IssueTracker
from autobot.audit import AuditLog, record_best_effort
from autobot.config import Config
from autobot.context import gather_context
from autobot.cost import CostLedger
from autobot.guardrails import detect_out_of_scope, guardrail_question
from autobot.implementation import ImplementationRunner
from autobot.labels import set_issue_label
from autobot.models import Issue, IssueRecord, IssueState, ProcessResult, utc_now
from autobot.result import abandon_process, finish_process, terminal_process_result
from autobot.state import StateStore
from autobot.workspace import branch_name, prepare_dry_run_repo, repo_work_dir


class IssueProcessor:
    def __init__(
        self,
        config: Config,
        store: StateStore,
        tracker: IssueTracker,
        git_host: GitHost,
        chat: ChatChannel,
        llm: LLM,
        audit: AuditLog,
    ) -> None:
        self.config = config
        self.store = store
        self.tracker = tracker
        self.git_host = git_host
        self.chat = chat
        self.llm = llm
        self.audit = audit
        self.comments_this_run = 0

    def process(self, repo: str, issue_number: int) -> ProcessResult:
        started = time.monotonic()
        self.comments_this_run = 0
        record = self.store.ensure(repo, issue_number)
        ledger = CostLedger(record.cost)

        if record.state == IssueState.ABANDONED:
            return terminal_process_result(
                record,
                "issue is abandoned; clear the state record before retrying",
            )

        if record.state == IssueState.PR_OPEN:
            return terminal_process_result(record, "draft pull request already open")

        if ledger.hit_budget(self.config.max_issue_tokens, self.config.max_issue_dollars):
            if record.blocked_on == "budget":
                return finish_process(
                    self.store, record, ledger, "waiting for budget increase", None, started
                )
            issue_ref = Issue(repo, issue_number, "", "", "unknown", [])
            try:
                self._pause_if_budget_hit(issue_ref, record, ledger, "run start")
            except resume.PausedForHuman as exc:
                return finish_process(self.store, record, ledger, str(exc), None, started)
            except Exception as exc:
                message = abandon_process(self.store, record, ledger, exc, started)
                raise RuntimeError(message) from exc

        issue = self.tracker.get(repo, issue_number)

        resumed = False
        previous_blocked_on = record.blocked_on
        if record.state == IssueState.WAITING:
            if record.blocked_on == "comment_limit":
                return self._resume_comment_limit_pause(issue, record, ledger, started)
            resumed, waiting_message = resume.resume_waiting(
                record,
                issue,
                self.config.agent_login,
                ledger,
                self.config.max_issue_tokens,
                self.config.max_issue_dollars,
            )
            if not resumed:
                return finish_process(self.store, record, ledger, waiting_message, None, started)
            self.store.upsert(record)

        topics = detect_out_of_scope(issue)
        if resumed and topics and previous_blocked_on == "out_of_scope":
            record.blocked_on = "out_of_scope"
            record.transition(IssueState.WAITING)
            self.store.upsert(record)
            return finish_process(
                self.store,
                record,
                ledger,
                "still waiting on out-of-scope guardrail",
                None,
                started,
            )
        if topics and "guardrail_pause" not in record.conversation:
            try:
                return self._pause_for_guardrail(issue, record, ledger, topics, started)
            except Exception as exc:
                message = abandon_process(self.store, record, ledger, exc, started)
                raise RuntimeError(message) from exc

        try:
            repo_dir = self._clone_and_branch(issue, record)
            context = gather_context(repo_dir, issue)

            if record.state not in {
                IssueState.SPEC_READY,
                IssueState.IMPLEMENTING,
                IssueState.REVIEW_LOOP,
                IssueState.PR_OPEN,
            }:
                triage = self.llm.triage(issue, context)
                ledger.add(triage.usage)
                record.transition(IssueState.TRIAGED)
                record.conversation["triage"] = {
                    "ready": triage.ready,
                    "questions": triage.questions,
                    "reason": triage.reason,
                    "at": utc_now(),
                }
                self.store.upsert(record)
                self._pause_if_budget_hit(issue, record, ledger, "triage")
                if not triage.ready:
                    if resumed and previous_blocked_on == "clarification":
                        resume.mark_clarification_still_needed(
                            record, triage.questions, triage.reason
                        )
                        self.store.upsert(record)
                        return finish_process(
                            self.store,
                            record,
                            ledger,
                            "still needs clarification after reply",
                            None,
                            started,
                        )
                    return self._ask_and_wait(issue, record, ledger, triage.questions, started)
                record.transition(IssueState.SPEC_READY)
                self.store.upsert(record)

            pr_url = self._implement_review_and_pr(issue, record, ledger, repo_dir)
        except resume.PausedForHuman as exc:
            return finish_process(self.store, record, ledger, str(exc), None, started)
        except Exception as exc:
            message = abandon_process(self.store, record, ledger, exc, started)
            raise RuntimeError(message) from exc
        return finish_process(
            self.store, record, ledger, "opened draft pull request", pr_url, started
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

    def _pause_for_guardrail(
        self,
        issue: Issue,
        record: IssueRecord,
        ledger: CostLedger,
        topics: list[str],
        started: float,
    ) -> ProcessResult:
        question = guardrail_question(topics)
        if self.comments_this_run >= self.config.comment_limit:
            return self._pause_for_comment_limit(
                issue,
                record,
                ledger,
                "guardrail",
                [question],
                started,
                topics,
            )
        if self.config.dry_run:
            comment_id = resume.latest_comment_id(issue)
        else:
            comment_id = self.chat.ask(issue, [question])
            self.comments_this_run += 1
        record.transition(IssueState.ASKED)
        record.conversation["guardrail_pause"] = {
            "topics": topics,
            "question": question,
            "comment_id": comment_id,
            "at": utc_now(),
        }
        record.conversation["resume_after_comment_id"] = comment_id
        record.blocked_on = "out_of_scope"
        self.store.upsert(record)
        record.transition(IssueState.WAITING)
        self.store.upsert(record)
        if not self.config.dry_run:
            record_best_effort(
                self.audit, "comment", issue.repo, issue.number, {"comment_id": comment_id}, record
            )
            set_issue_label(self.tracker, self.audit, record, issue, "agent-waiting")
            self.store.upsert(record)
        return finish_process(
            self.store, record, ledger, "paused for out-of-scope guardrail", None, started
        )

    def _ask_and_wait(
        self,
        issue: Issue,
        record: IssueRecord,
        ledger: CostLedger,
        questions: list[str],
        started: float,
    ) -> ProcessResult:
        record.transition(IssueState.NEEDS_SPEC)
        self.store.upsert(record)
        if self.comments_this_run >= self.config.comment_limit:
            return self._pause_for_comment_limit(
                issue,
                record,
                ledger,
                "clarification",
                questions[:3],
                started,
            )
        if self.config.dry_run:
            comment_id = resume.latest_comment_id(issue)
        else:
            comment_id = self.chat.ask(issue, questions[:3])
            self.comments_this_run += 1
        record.transition(IssueState.ASKED)
        record.conversation["asked_comment_id"] = comment_id
        record.conversation["asked_at"] = utc_now()
        record.conversation["asked_questions"] = questions[:3]
        record.conversation["resume_after_comment_id"] = comment_id
        self.store.upsert(record)
        record.blocked_on = "clarification"
        record.transition(IssueState.WAITING)
        self.store.upsert(record)
        if not self.config.dry_run:
            record_best_effort(
                self.audit, "comment", issue.repo, issue.number, {"comment_id": comment_id}, record
            )
            set_issue_label(self.tracker, self.audit, record, issue, "agent-waiting")
            self.store.upsert(record)
        return finish_process(
            self.store,
            record,
            ledger,
            "posted clarification and entered waiting",
            None,
            started,
        )

    def _pause_for_comment_limit(
        self,
        issue: Issue,
        record: IssueRecord,
        ledger: CostLedger,
        kind: str,
        questions: list[str],
        started: float,
        topics: list[str] | None = None,
    ) -> ProcessResult:
        record.conversation["comment_limit_pause"] = {
            "kind": kind,
            "questions": questions,
            "topics": topics or [],
            "at": utc_now(),
        }
        record.conversation["resume_after_comment_id"] = resume.latest_comment_id(issue)
        record.blocked_on = "comment_limit"
        record.transition(IssueState.WAITING)
        return finish_process(
            self.store,
            record,
            ledger,
            "waiting for outbound comment capacity",
            None,
            started,
        )

    def _resume_comment_limit_pause(
        self,
        issue: Issue,
        record: IssueRecord,
        ledger: CostLedger,
        started: float,
    ) -> ProcessResult:
        if self.comments_this_run >= self.config.comment_limit:
            return finish_process(
                self.store,
                record,
                ledger,
                "waiting for outbound comment capacity",
                None,
                started,
            )
        pause = record.conversation.pop("comment_limit_pause", {})
        if pause.get("kind") == "guardrail":
            return self._pause_for_guardrail(
                issue,
                record,
                ledger,
                list(pause.get("topics") or []),
                started,
            )
        return self._ask_and_wait(
            issue,
            record,
            ledger,
            list(pause.get("questions") or []),
            started,
        )

    def _implement_review_and_pr(
        self,
        issue: Issue,
        record: IssueRecord,
        ledger: CostLedger,
        repo_dir: Path,
    ) -> str | None:
        runner = ImplementationRunner(
            self.config,
            self.store,
            self.tracker,
            self.git_host,
            self.llm,
            self.audit,
            self._pause_if_budget_hit,
        )
        return runner.run(issue, record, ledger, repo_dir)

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
        pause = {"phase": phase, "at": utc_now(), "cost": ledger.to_dict()}
        record.conversation["budget_pause"] = pause
        record.conversation["resume_after_comment_id"] = 0
        record.blocked_on = "budget"
        comment_id: int | None = None
        if not self.config.dry_run:
            if self.comments_this_run >= self.config.comment_limit:
                pause["comment_skipped"] = "comment_limit"
            else:
                comment_id = self.chat.notify(issue, text)
                self.comments_this_run += 1
                pause["comment_id"] = comment_id
                record.conversation["resume_after_comment_id"] = comment_id
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
