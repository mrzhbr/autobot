from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from autobot.audit import AuditLog
from autobot.chat import IssueCommentChat
from autobot.config import Config
from autobot.github import GitHubGitHost
from autobot.llm import MockLLM
from autobot.models import (
    ContextFile,
    FileChange,
    ImplementationPlan,
    Issue,
    IssueComment,
    IssueState,
    ReviewReport,
    TriageDecision,
    Usage,
)
from autobot.pipeline import IssueProcessor
from autobot.state import StateStore


class FakeTracker:
    def __init__(
        self,
        title: str = "Add demo behavior",
        body: str = "Please clarify before implementing.",
    ) -> None:
        self.comments: list[IssueComment] = []
        self.title = title
        self.body = body

    def list_actionable(self, repo: str) -> list[int]:
        return [1]

    def get(self, repo: str, issue_number: int) -> Issue:
        return Issue(
            repo=repo,
            number=issue_number,
            title=self.title,
            body=self.body,
            author="alice",
            labels=[],
            comments=list(self.comments),
        )

    def comment(self, repo: str, issue_number: int, text: str) -> int:
        comment_id = len(self.comments) + 1
        self.comments.append(IssueComment(comment_id, "bot", text, "2026-06-05T00:00:00Z"))
        return comment_id

    def set_label(self, repo: str, issue_number: int, label: str) -> None:
        return None


class SequencedLLM:
    def __init__(self) -> None:
        self.triage_calls = 0

    def triage(self, issue: Issue, context: list[ContextFile]) -> TriageDecision:
        self.triage_calls += 1
        if self.triage_calls == 1:
            return TriageDecision(False, ["Which behavior should be used?"], "Missing choice.")
        return TriageDecision(True, [], "Human answered.")

    def implement(
        self,
        issue: Issue,
        context: list[ContextFile],
        review_findings: list[str] | None = None,
    ) -> ImplementationPlan:
        return ImplementationPlan(
            plan=["Write the clarified behavior into README.md."],
            changes=[FileChange("README.md", "# Dry run repo\n\nImplemented.\n")],
            test_commands=["true"],
        )

    def review(
        self,
        lens: str,
        issue: Issue,
        diff: str,
        model: str | None = None,
    ) -> ReviewReport:
        return ReviewReport(lens, [], Usage("review", model or "default-review", 0, 0, 0))


class BudgetLLM(SequencedLLM):
    def triage(self, issue: Issue, context: list[ContextFile]) -> TriageDecision:
        return TriageDecision(
            ready=True,
            questions=[],
            reason="Ready but expensive.",
            usage=Usage("triage", "test", 2, 0),
        )


class AlwaysNotReadyLLM(SequencedLLM):
    def triage(self, issue: Issue, context: list[ContextFile]) -> TriageDecision:
        self.triage_calls += 1
        return TriageDecision(False, ["Still unclear."], "The answer did not resolve the spec.")


class PipelineTests(unittest.TestCase):
    def test_waiting_state_resumes_after_human_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.from_env(root=root, dry_run=True, mock_llm=True)
            tracker = FakeTracker()
            store = StateStore(config.db_path)
            llm = SequencedLLM()
            processor = IssueProcessor(
                config=config,
                store=store,
                tracker=tracker,
                git_host=GitHubGitHost(None),
                chat=IssueCommentChat(tracker),
                llm=llm,
                audit=AuditLog(config.audit_path),
            )

            first = processor.process("owner/repo", 1)
            self.assertEqual(first.state, IssueState.WAITING)

            tracker.comments.append(
                IssueComment(1, "alice", "Use the compact option.", "2026-06-05T00:01:00Z")
            )
            second = processor.process("owner/repo", 1)

            self.assertEqual(second.state, IssueState.PR_OPEN)
            self.assertEqual(second.pr_url, "dry-run://draft-pr")
            self.assertEqual(second.files_touched, ["README.md"])
            self.assertTrue(second.branch.startswith("autobot/issue-1-"))
            self.assertEqual(second.review_rounds, 1)
            loaded = store.get("owner/repo", 1)
            assert loaded is not None
            self.assertEqual(loaded.conversation["human_replies"][0]["author"], "alice")
            self.assertEqual(
                loaded.conversation["asked_questions"],
                ["Which behavior should be used?"],
            )
            self.assertEqual(loaded.conversation["pr_url"], "dry-run://draft-pr")
            self.assertEqual(loaded.conversation["ci_status"]["state"], "dry-run")
            self.assertEqual(loaded.plan["verification_commands"], ["true"])

    def test_pr_open_rerun_returns_stored_pr_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.from_env(root=root, dry_run=True, mock_llm=True)
            tracker = FakeTracker(body="Ready to implement.")
            store = StateStore(config.db_path)
            processor = IssueProcessor(
                config=config,
                store=store,
                tracker=tracker,
                git_host=GitHubGitHost(None),
                chat=IssueCommentChat(tracker),
                llm=MockLLM(),
                audit=AuditLog(config.audit_path),
            )

            first = processor.process("owner/repo", 1)
            second = processor.process("owner/repo", 1)

            self.assertEqual(first.state, IssueState.PR_OPEN)
            self.assertEqual(second.state, IssueState.PR_OPEN)
            self.assertEqual(second.message, "draft pull request already open")
            self.assertEqual(second.pr_url, "dry-run://draft-pr")

    def test_budget_hit_pauses_in_waiting_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = replace(
                Config.from_env(root=root, dry_run=True, mock_llm=True),
                max_issue_tokens=1,
            )
            tracker = FakeTracker()
            store = StateStore(config.db_path)
            processor = IssueProcessor(
                config=config,
                store=store,
                tracker=tracker,
                git_host=GitHubGitHost(None),
                chat=IssueCommentChat(tracker),
                llm=BudgetLLM(),
                audit=AuditLog(config.audit_path),
            )

            result = processor.process("owner/repo", 1)

            self.assertEqual(result.state, IssueState.WAITING)
            self.assertIn("budget", result.message)
            loaded = store.get("owner/repo", 1)
            assert loaded is not None
            self.assertEqual(loaded.blocked_on, "budget")
            self.assertEqual(loaded.conversation["budget_pause"]["phase"], "triage")

    def test_clarification_reply_reruns_triage_once_without_second_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.from_env(root=root, dry_run=True, mock_llm=True)
            tracker = FakeTracker()
            store = StateStore(config.db_path)
            llm = AlwaysNotReadyLLM()
            processor = IssueProcessor(
                config=config,
                store=store,
                tracker=tracker,
                git_host=GitHubGitHost(None),
                chat=IssueCommentChat(tracker),
                llm=llm,
                audit=AuditLog(config.audit_path),
            )

            first = processor.process("owner/repo", 1)
            tracker.comments.append(
                IssueComment(1, "alice", "Not enough detail.", "2026-06-05T00:01:00Z")
            )
            second = processor.process("owner/repo", 1)

            self.assertEqual(first.state, IssueState.WAITING)
            self.assertEqual(second.state, IssueState.WAITING)
            self.assertEqual(second.message, "still needs clarification after reply")
            self.assertEqual(llm.triage_calls, 2)
            loaded = store.get("owner/repo", 1)
            assert loaded is not None
            self.assertEqual(loaded.blocked_on, "clarification")
            self.assertEqual(
                loaded.conversation["clarification_after_resume"]["questions"],
                ["Still unclear."],
            )

    def test_out_of_scope_issue_pauses_before_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.from_env(root=root, dry_run=True, mock_llm=True)
            tracker = FakeTracker(
                title="Add OAuth login",
                body="Implement authentication for the app.",
            )
            store = StateStore(config.db_path)
            llm = SequencedLLM()
            processor = IssueProcessor(
                config=config,
                store=store,
                tracker=tracker,
                git_host=GitHubGitHost(None),
                chat=IssueCommentChat(tracker),
                llm=llm,
                audit=AuditLog(config.audit_path),
            )

            result = processor.process("owner/repo", 1)

            self.assertEqual(result.state, IssueState.WAITING)
            self.assertEqual(result.blocked_on, "out_of_scope")
            self.assertEqual(llm.triage_calls, 0)
            loaded = store.get("owner/repo", 1)
            assert loaded is not None
            self.assertIn("authentication", loaded.conversation["guardrail_pause"]["topics"])

    def test_waiting_guardrail_ignores_comments_before_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.from_env(root=root, dry_run=True, mock_llm=True)
            tracker = FakeTracker(
                title="Add OAuth login",
                body="Implement authentication for the app.",
            )
            tracker.comments.append(
                IssueComment(2, "alice", "Older issue discussion.", "2026-06-05T00:00:00Z")
            )
            store = StateStore(config.db_path)
            record = store.ensure("owner/repo", 1)
            record.transition(IssueState.WAITING)
            record.blocked_on = "out_of_scope"
            record.conversation["guardrail_pause"] = {"comment_id": 5}
            store.upsert(record)
            llm = SequencedLLM()
            processor = IssueProcessor(
                config=config,
                store=store,
                tracker=tracker,
                git_host=GitHubGitHost(None),
                chat=IssueCommentChat(tracker),
                llm=llm,
                audit=AuditLog(config.audit_path),
            )

            result = processor.process("owner/repo", 1)

            self.assertEqual(result.state, IssueState.WAITING)
            self.assertEqual(result.blocked_on, "out_of_scope")
            self.assertEqual(llm.triage_calls, 0)

    def test_guardrail_reply_does_not_resume_unchanged_out_of_scope_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.from_env(root=root, dry_run=True, mock_llm=True)
            tracker = FakeTracker(
                title="Add OAuth login",
                body="Implement authentication for the app.",
            )
            tracker.comments.append(
                IssueComment(6, "alice", "Please do it anyway.", "2026-06-05T00:01:00Z")
            )
            store = StateStore(config.db_path)
            record = store.ensure("owner/repo", 1)
            record.transition(IssueState.WAITING)
            record.blocked_on = "out_of_scope"
            record.conversation["guardrail_pause"] = {"comment_id": 5}
            record.conversation["resume_after_comment_id"] = 5
            store.upsert(record)
            llm = SequencedLLM()
            processor = IssueProcessor(
                config=config,
                store=store,
                tracker=tracker,
                git_host=GitHubGitHost(None),
                chat=IssueCommentChat(tracker),
                llm=llm,
                audit=AuditLog(config.audit_path),
            )

            result = processor.process("owner/repo", 1)

            self.assertEqual(result.state, IssueState.WAITING)
            self.assertEqual(result.blocked_on, "out_of_scope")
            self.assertEqual(result.message, "still waiting on out-of-scope guardrail")
            self.assertEqual(llm.triage_calls, 0)


if __name__ == "__main__":
    unittest.main()
