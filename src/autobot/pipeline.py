from __future__ import annotations

import time
from pathlib import Path

from autobot import resume
from autobot import sandbox as sandbox_ops
from autobot.adapters import LLM, ChatChannel, GitHost, IssueTracker
from autobot.audit import AuditLog
from autobot.config import Config
from autobot.context import gather_context
from autobot.cost import CostLedger
from autobot.guardrails import detect_out_of_scope, guardrail_question
from autobot.models import Issue, IssueRecord, IssueState, ProcessResult, utc_now
from autobot.pr import build_pr_body
from autobot.result import finish_process
from autobot.review import ReviewerPanel, format_blockers
from autobot.scanner import find_secret_like_values, redact_secret_like_values
from autobot.state import StateStore
from autobot.tests import detect_verification_commands, merge_verification_commands
from autobot.workspace import branch_name, changed_files, prepare_dry_run_repo, repo_work_dir


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
        issue = self.tracker.get(repo, issue_number)
        record = self.store.ensure(repo, issue_number)
        ledger = CostLedger(record.cost)

        if record.state == IssueState.ABANDONED:
            return finish_process(
                self.store,
                record,
                ledger,
                "issue is abandoned; clear the state record before retrying",
                None,
                started,
            )

        resumed = False
        previous_blocked_on = record.blocked_on
        if record.state == IssueState.WAITING:
            resumed = resume.resume_if_answered(
                record, issue, self.config.agent_login
            ) or resume.resume_if_budget_allows(
                record,
                ledger,
                self.config.max_issue_tokens,
                self.config.max_issue_dollars,
            )
            if not resumed:
                return finish_process(
                    self.store, record, ledger, "waiting for a human answer", None, started
                )
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
            return self._pause_for_guardrail(issue, record, ledger, topics, started)

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
            try:
                self._pause_if_budget_hit(issue, record, ledger, "triage")
            except resume.PausedForHuman as exc:
                return finish_process(self.store, record, ledger, str(exc), None, started)
            if not triage.ready:
                if resumed and previous_blocked_on == "clarification":
                    resume.mark_clarification_still_needed(record, triage.questions, triage.reason)
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

        if record.state == IssueState.PR_OPEN:
            return finish_process(
                self.store, record, ledger, "draft pull request already open", None, started
            )

        try:
            pr_url = self._implement_review_and_pr(issue, record, ledger, repo_dir)
        except resume.PausedForHuman as exc:
            return finish_process(self.store, record, ledger, str(exc), None, started)
        except Exception as exc:
            message = redact_secret_like_values(str(exc))
            record.transition(IssueState.ABANDONED)
            record.blocked_on = message
            finish_process(self.store, record, ledger, message, None, started)
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
            raise RuntimeError("comment limit reached before guardrail question could be posted")
        if self.config.dry_run:
            comment_id = 0
        else:
            comment_id = self.chat.ask(issue, [question])
            self.comments_this_run += 1
            self.tracker.set_label(issue.repo, issue.number, "agent-waiting")
            self.audit.record("comment", issue.repo, issue.number, {"comment_id": comment_id})
            self.audit.record("label", issue.repo, issue.number, {"label": "agent-waiting"})
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
            raise RuntimeError("comment limit reached before clarification could be posted")
        if self.config.dry_run:
            comment_id = 0
        else:
            comment_id = self.chat.ask(issue, questions[:3])
            self.comments_this_run += 1
            self.tracker.set_label(issue.repo, issue.number, "agent-waiting")
            self.audit.record("comment", issue.repo, issue.number, {"comment_id": comment_id})
            self.audit.record("label", issue.repo, issue.number, {"label": "agent-waiting"})
        record.transition(IssueState.ASKED)
        record.conversation["asked_comment_id"] = comment_id
        record.conversation["asked_at"] = utc_now()
        record.conversation["asked_questions"] = questions[:3]
        record.conversation["resume_after_comment_id"] = comment_id
        self.store.upsert(record)
        record.blocked_on = "clarification"
        record.transition(IssueState.WAITING)
        self.store.upsert(record)
        return finish_process(
            self.store,
            record,
            ledger,
            "posted clarification and entered waiting",
            None,
            started,
        )

    def _implement_review_and_pr(
        self,
        issue: Issue,
        record: IssueRecord,
        ledger: CostLedger,
        repo_dir: Path,
    ) -> str | None:
        record.transition(IssueState.IMPLEMENTING)
        self.store.upsert(record)
        dry_run = self.config.dry_run
        setup_command = sandbox_ops.detect_setup_command(
            repo_dir, self.config.sandbox_setup_command
        )
        sandbox = sandbox_ops.DockerSandbox(
            repo_dir,
            self.config.sandbox_image,
            setup_command,
            self.config.sandbox_network,
        )
        if not dry_run:
            self.tracker.set_label(issue.repo, issue.number, "agent-working")
            self.audit.record("label", issue.repo, issue.number, {"label": "agent-working"})
            sandbox.prepare()

        test_plan = self.llm.write_tests(issue, gather_context(repo_dir, issue))
        ledger.add(test_plan.usage)
        self._pause_if_budget_hit(issue, record, ledger, "test authoring")
        if not test_plan.changes:
            raise RuntimeError("test author returned no changes")
        sandbox_ops.apply_changes(repo_dir, sandbox, test_plan.changes, dry_run)
        baseline = sandbox_ops.run_verification_allow_failure(
            sandbox, test_plan.test_commands, dry_run
        )
        plan = self.llm.implement(issue, gather_context(repo_dir, issue))
        ledger.add(plan.usage)
        self._pause_if_budget_hit(issue, record, ledger, "implementation")
        if not plan.changes:
            raise RuntimeError("implementer returned no changes")
        merge = merge_verification_commands
        all_changes = [*test_plan.changes, *plan.changes]
        impl_commands = list(plan.test_commands)
        record.plan = {
            "acceptance_tests": test_plan.plan,
            "acceptance_test_baseline": baseline,
            "plan": plan.plan,
            "test_author_commands": test_plan.test_commands,
            "test_commands": plan.test_commands,
            "at": utc_now(),
        }
        self.store.upsert(record)
        sandbox_ops.apply_changes(repo_dir, sandbox, plan.changes, dry_run)
        detected = detect_verification_commands(repo_dir, self.config.default_test_command)
        verification_commands = merge(test_plan.test_commands, impl_commands, detected)
        record.plan["verification_commands"] = verification_commands
        self.store.upsert(record)
        test_output = sandbox_ops.run_verification(sandbox, verification_commands, dry_run)

        record.transition(IssueState.REVIEW_LOOP)
        self.store.upsert(record)
        panel = ReviewerPanel(self.llm, models=self.config.review_models)
        for round_number in range(1, self.config.max_review_rounds + 1):
            record.review_rounds = round_number
            diff = self.git_host.current_diff(repo_dir)
            outcome = panel.review(issue, diff, ledger)
            self._pause_if_budget_hit(issue, record, ledger, "review")
            self.store.upsert(record)
            if not outcome.blocking_findings:
                break
            if round_number >= self.config.max_review_rounds:
                raise RuntimeError("review loop stopped with blocking findings")
            fix = self.llm.implement(
                issue,
                gather_context(repo_dir, issue),
                format_blockers(outcome.blocking_findings),
            )
            ledger.add(fix.usage)
            self._pause_if_budget_hit(issue, record, ledger, "review fix")
            if not fix.changes:
                raise RuntimeError("implementer returned no fixes for blocking findings")
            all_changes.extend(fix.changes)
            impl_commands.extend(fix.test_commands)
            sandbox_ops.apply_changes(repo_dir, sandbox, fix.changes, dry_run)
            verification_commands = merge(test_plan.test_commands, impl_commands, detected)
            record.plan["verification_commands"] = verification_commands
            self.store.upsert(record)
            test_output = sandbox_ops.run_verification(sandbox, verification_commands, dry_run)
        diff = self.git_host.current_diff(repo_dir)
        secrets = find_secret_like_values(diff)
        if secrets:
            raise RuntimeError(f"secret-like values found in diff: {secrets[:3]}")
        if dry_run:
            record.files_touched = [change.path for change in all_changes]
            record.conversation["ci_status"] = {"state": "dry-run"}
            record.conversation["pr_url"] = "dry-run://draft-pr"
            record.pr_url = "dry-run://draft-pr"
            record.transition(IssueState.PR_OPEN)
            record.cost = ledger.to_dict()
            self.store.upsert(record)
            return "dry-run://draft-pr"
        committed = self.git_host.commit_all(repo_dir, f"feat: implement issue #{issue.number}")
        if not committed:
            raise RuntimeError("no changes to commit")
        branch = record.branch or branch_name(issue)
        self.git_host.push(issue.repo, repo_dir, branch)
        self.audit.record("push", issue.repo, issue.number, {"branch": branch})
        ci_status = self.git_host.ci_status(issue.repo, branch)
        record.conversation["ci_status"] = ci_status
        pr_url = self.git_host.open_draft_pr(
            issue.repo,
            branch,
            f"Draft: {issue.title}",
            build_pr_body(issue, record, ledger, verification_commands, test_output, ci_status),
        )
        self.audit.record("draft_pr", issue.repo, issue.number, {"url": pr_url, "branch": branch})
        self.tracker.set_label(issue.repo, issue.number, "agent-pr-open")
        self.audit.record("label", issue.repo, issue.number, {"label": "agent-pr-open"})
        record.conversation["pr_url"] = pr_url
        record.pr_url = pr_url
        record.files_touched = changed_files(repo_dir)
        record.transition(IssueState.PR_OPEN)
        record.cost = ledger.to_dict()
        self.store.upsert(record)
        return pr_url

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
        if not self.config.dry_run:
            if self.comments_this_run >= self.config.comment_limit:
                raise RuntimeError("comment limit reached before budget pause could be posted")
            comment_id = self.chat.notify(issue, text)
            self.comments_this_run += 1
            self.tracker.set_label(issue.repo, issue.number, "agent-waiting")
            self.audit.record("comment", issue.repo, issue.number, {"comment_id": comment_id})
            self.audit.record("label", issue.repo, issue.number, {"label": "agent-waiting"})
            pause["comment_id"] = comment_id
            record.conversation["resume_after_comment_id"] = comment_id
        record.transition(IssueState.WAITING)
        self.store.upsert(record)
        raise resume.PausedForHuman(text)
