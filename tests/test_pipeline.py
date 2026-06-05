from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    ReviewFinding,
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


class FakeGitHost:
    def clone(self, repo: str, target_dir: Path) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "README.md").write_text("# Repo\n", encoding="utf-8")

    def create_branch(self, repo_dir: Path, branch: str) -> None:
        return None

    def current_diff(self, repo_dir: Path) -> str:
        return ""

    def commit_all(self, repo_dir: Path, message: str) -> bool:
        return True

    def push(self, repo: str, repo_dir: Path, branch: str) -> None:
        return None

    def ci_status(self, repo: str, branch: str) -> dict:
        return {"state": "success"}

    def open_draft_pr(self, repo: str, branch: str, title: str, body: str) -> str:
        return "https://github.test/pull/1"


class PackageLockGitHost(FakeGitHost):
    def clone(self, repo: str, target_dir: Path) -> None:
        super().clone(repo, target_dir)
        (target_dir / "package.json").write_text("{}", encoding="utf-8")
        (target_dir / "package-lock.json").write_text("{}", encoding="utf-8")


class SecretFailGitHost(FakeGitHost):
    def __init__(self, token: str) -> None:
        self.token = token

    def current_diff(self, repo_dir: Path) -> str:
        raise RuntimeError(f"git failed with {self.token}")


class SequencedLLM:
    def __init__(self) -> None:
        self.triage_calls = 0
        self.test_author_calls = 0

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

    def write_tests(self, issue: Issue, context: list[ContextFile]) -> ImplementationPlan:
        self.test_author_calls += 1
        return ImplementationPlan(
            plan=["Write acceptance test."],
            changes=[
                FileChange(
                    f"tests/test_issue_{issue.number}.py",
                    "def test_acceptance():\n    assert True\n",
                )
            ],
            test_commands=["python -m pytest"],
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


class CommentAwareLLM(SequencedLLM):
    def triage(self, issue: Issue, context: list[ContextFile]) -> TriageDecision:
        self.triage_calls += 1
        if any(
            comment.author == "alice" and "compact" in comment.body for comment in issue.comments
        ):
            return TriageDecision(True, [], "Human answered.")
        return TriageDecision(False, ["Which behavior should be used?"], "Missing choice.")


class BlockingFixLLM(SequencedLLM):
    def __init__(self) -> None:
        super().__init__()
        self.review_calls = 0

    def triage(self, issue: Issue, context: list[ContextFile]) -> TriageDecision:
        return TriageDecision(True, [], "Ready.")

    def implement(
        self,
        issue: Issue,
        context: list[ContextFile],
        review_findings: list[str] | None = None,
    ) -> ImplementationPlan:
        if review_findings:
            return ImplementationPlan(
                plan=["Fix the blocking review finding."],
                changes=[FileChange("docs/fix.md", "Fixed.\n")],
                test_commands=["python -m pytest -q"],
            )
        return ImplementationPlan(
            plan=["Write initial behavior."],
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
        self.review_calls += 1
        if self.review_calls == 1:
            finding = ReviewFinding("high", "README.md", 1, "Needs a follow-up fix.", True)
            return ReviewReport(lens, [finding], Usage("review", model or "review", 0, 0, 0))
        return ReviewReport(lens, [], Usage("review", model or "review", 0, 0, 0))


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
            self.assertEqual(second.files_touched, ["tests/test_issue_1.py", "README.md"])
            self.assertTrue(second.branch.startswith("autobot/issue-1-"))
            self.assertEqual(second.review_rounds, 1)
            self.assertEqual(llm.test_author_calls, 1)
            loaded = store.get("owner/repo", 1)
            assert loaded is not None
            self.assertEqual(loaded.conversation["human_replies"][0]["author"], "alice")
            self.assertEqual(
                loaded.conversation["asked_questions"],
                ["Which behavior should be used?"],
            )
            self.assertEqual(loaded.conversation["pr_url"], "dry-run://draft-pr")
            self.assertEqual(loaded.pr_url, "dry-run://draft-pr")
            self.assertEqual(loaded.conversation["ci_status"]["state"], "dry-run")
            self.assertEqual(loaded.plan["acceptance_tests"], ["Write acceptance test."])
            self.assertEqual(loaded.plan["acceptance_test_baseline"]["ok"], True)
            self.assertIn("dry-run skipped", loaded.plan["acceptance_test_baseline"]["output"])
            self.assertEqual(
                loaded.plan["verification_commands"],
                ["python -m pytest", "true", "python -m unittest discover -s tests"],
            )

    def test_waiting_state_survives_store_and_processor_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.from_env(root=root, dry_run=True, mock_llm=True)
            tracker = FakeTracker()
            store = StateStore(config.db_path)
            first = IssueProcessor(
                config=config,
                store=store,
                tracker=tracker,
                git_host=GitHubGitHost(None),
                chat=IssueCommentChat(tracker),
                llm=CommentAwareLLM(),
                audit=AuditLog(config.audit_path),
            ).process("owner/repo", 1)
            self.assertEqual(first.state, IssueState.WAITING)

            tracker.comments.append(
                IssueComment(2, "alice", "Use the compact option.", "2026-06-05T00:01:00Z")
            )
            restarted_store = StateStore(config.db_path)
            restarted = IssueProcessor(
                config=config,
                store=restarted_store,
                tracker=tracker,
                git_host=GitHubGitHost(None),
                chat=IssueCommentChat(tracker),
                llm=CommentAwareLLM(),
                audit=AuditLog(config.audit_path),
            ).process("owner/repo", 1)

            self.assertEqual(restarted.state, IssueState.PR_OPEN)
            loaded = restarted_store.get("owner/repo", 1)
            assert loaded is not None
            self.assertEqual(loaded.conversation["human_replies"][0]["id"], 2)
            self.assertEqual(
                loaded.conversation["asked_questions"],
                ["Which behavior should be used?"],
            )

    def test_review_fix_commands_and_files_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.from_env(root=root, dry_run=True, mock_llm=True)
            tracker = FakeTracker(body="Ready to implement.")
            store = StateStore(config.db_path)
            llm = BlockingFixLLM()
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

            self.assertEqual(result.state, IssueState.PR_OPEN)
            self.assertEqual(result.review_rounds, 2)
            self.assertEqual(
                result.files_touched,
                ["tests/test_issue_1.py", "README.md", "docs/fix.md"],
            )
            loaded = store.get("owner/repo", 1)
            assert loaded is not None
            self.assertEqual(
                loaded.plan["verification_commands"],
                [
                    "python -m pytest",
                    "true",
                    "python -m pytest -q",
                    "python -m unittest discover -s tests",
                ],
            )

    def test_live_pipeline_uses_detected_sandbox_setup_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.from_env(root=root, dry_run=False, mock_llm=True)
            tracker = FakeTracker(body="Ready to implement.")
            store = StateStore(config.db_path)
            completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
            processor = IssueProcessor(
                config=config,
                store=store,
                tracker=tracker,
                git_host=PackageLockGitHost(),
                chat=IssueCommentChat(tracker),
                llm=MockLLM(),
                audit=AuditLog(config.audit_path),
            )

            with patch("autobot.sandbox.subprocess.run", return_value=completed) as run:
                result = processor.process("owner/repo", 1)

            first_docker_command = run.call_args_list[0].args[0]
            self.assertEqual(result.state, IssueState.PR_OPEN)
            self.assertEqual(first_docker_command[-1], "npm ci")

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
            loaded = store.get("owner/repo", 1)
            assert loaded is not None
            self.assertEqual(loaded.pr_url, "dry-run://draft-pr")

    def test_abandoned_rerun_does_not_restart_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config.from_env(root=root, dry_run=True, mock_llm=True)
            tracker = FakeTracker(body="Ready to implement.")
            store = StateStore(config.db_path)
            record = store.ensure("owner/repo", 1)
            record.transition(IssueState.ABANDONED)
            record.blocked_on = "sandbox command failed"
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

            self.assertEqual(result.state, IssueState.ABANDONED)
            self.assertEqual(result.blocked_on, "sandbox command failed")
            self.assertIn("clear the state record", result.message)
            self.assertEqual(llm.triage_calls, 0)
            self.assertFalse(config.work_root.exists())

    def test_abandoned_error_redacts_token_like_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "ghp_" + ("A" * 36)
            config = Config.from_env(root=root, dry_run=True, mock_llm=True)
            tracker = FakeTracker(body="Ready to implement.")
            store = StateStore(config.db_path)
            processor = IssueProcessor(
                config=config,
                store=store,
                tracker=tracker,
                git_host=SecretFailGitHost(token),
                chat=IssueCommentChat(tracker),
                llm=MockLLM(),
                audit=AuditLog(config.audit_path),
            )

            with self.assertRaises(RuntimeError) as raised:
                processor.process("owner/repo", 1)

            loaded = store.get("owner/repo", 1)
            assert loaded is not None
            self.assertEqual(loaded.state, IssueState.ABANDONED)
            self.assertNotIn(token, str(raised.exception))
            self.assertNotIn(token, loaded.blocked_on or "")
            self.assertIn("[redacted-secret]", loaded.blocked_on or "")

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

    def test_budget_pause_resumes_after_budget_is_increased(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            low_budget = replace(
                Config.from_env(root=root, dry_run=True, mock_llm=True),
                max_issue_tokens=1,
            )
            tracker = FakeTracker()
            store = StateStore(low_budget.db_path)
            first_llm = BudgetLLM()
            first_processor = IssueProcessor(
                config=low_budget,
                store=store,
                tracker=tracker,
                git_host=GitHubGitHost(None),
                chat=IssueCommentChat(tracker),
                llm=first_llm,
                audit=AuditLog(low_budget.audit_path),
            )

            first = first_processor.process("owner/repo", 1)
            high_budget = replace(low_budget, max_issue_tokens=10)
            second_processor = IssueProcessor(
                config=high_budget,
                store=StateStore(high_budget.db_path),
                tracker=tracker,
                git_host=GitHubGitHost(None),
                chat=IssueCommentChat(tracker),
                llm=BudgetLLM(),
                audit=AuditLog(high_budget.audit_path),
            )
            second = second_processor.process("owner/repo", 1)

            self.assertEqual(first.state, IssueState.WAITING)
            self.assertEqual(second.state, IssueState.PR_OPEN)
            self.assertEqual(second.pr_url, "dry-run://draft-pr")
            loaded = StateStore(high_budget.db_path).get("owner/repo", 1)
            assert loaded is not None
            self.assertIn("budget_resumed_at", loaded.conversation)

    def test_comment_limit_blocks_guardrail_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = replace(
                Config.from_env(root=root, dry_run=False, mock_llm=True),
                comment_limit=0,
            )
            tracker = FakeTracker(
                title="Add OAuth login",
                body="Implement authentication for the app.",
            )
            processor = IssueProcessor(
                config=config,
                store=StateStore(config.db_path),
                tracker=tracker,
                git_host=FakeGitHost(),
                chat=IssueCommentChat(tracker),
                llm=SequencedLLM(),
                audit=AuditLog(config.audit_path),
            )

            with self.assertRaisesRegex(RuntimeError, "comment limit reached"):
                processor.process("owner/repo", 1)

            self.assertEqual(tracker.comments, [])

    def test_comment_limit_blocks_clarification_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = replace(
                Config.from_env(root=root, dry_run=False, mock_llm=True),
                comment_limit=0,
            )
            tracker = FakeTracker()
            processor = IssueProcessor(
                config=config,
                store=StateStore(config.db_path),
                tracker=tracker,
                git_host=FakeGitHost(),
                chat=IssueCommentChat(tracker),
                llm=SequencedLLM(),
                audit=AuditLog(config.audit_path),
            )

            with self.assertRaisesRegex(RuntimeError, "comment limit reached"):
                processor.process("owner/repo", 1)

            self.assertEqual(tracker.comments, [])

    def test_comment_limit_resets_for_each_processed_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = replace(
                Config.from_env(root=root, dry_run=False, mock_llm=True),
                comment_limit=1,
            )
            tracker = FakeTracker()
            processor = IssueProcessor(
                config=config,
                store=StateStore(config.db_path),
                tracker=tracker,
                git_host=FakeGitHost(),
                chat=IssueCommentChat(tracker),
                llm=AlwaysNotReadyLLM(),
                audit=AuditLog(config.audit_path),
            )

            first = processor.process("owner/repo", 1)
            second = processor.process("owner/repo", 2)

            self.assertEqual(first.state, IssueState.WAITING)
            self.assertEqual(second.state, IssueState.WAITING)
            self.assertEqual(len(tracker.comments), 2)

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
