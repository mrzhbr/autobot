from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from autobot import sandbox as sandbox_ops
from autobot.adapters import LLM, GitHost, IssueTracker
from autobot.audit import AuditLog
from autobot.config import Config
from autobot.context import gather_context
from autobot.cost import CostLedger
from autobot.labels import set_issue_label
from autobot.models import FileChange, Issue, IssueRecord, IssueState, utc_now
from autobot.pr_flow import finalize_draft_pr
from autobot.review import ReviewerPanel, format_blockers, review_round_artifact
from autobot.scanner import ensure_no_secret_like_values
from autobot.state import StateStore
from autobot.tests import VerificationCommands, detect_verification_commands
from autobot.tests import merge_verification_commands as merge_commands

BudgetPause = Callable[[Issue, IssueRecord, CostLedger, str], None]


@dataclass
class WorkArtifacts:
    all_changes: list[FileChange]
    authored_commands: list[str]
    impl_commands: list[str]
    detected: VerificationCommands
    verification_commands: list[str]
    test_output: str


class ImplementationRunner:
    def __init__(
        self,
        config: Config,
        store: StateStore,
        tracker: IssueTracker,
        git_host: GitHost,
        llm: LLM,
        audit: AuditLog,
        pause_if_budget_hit: BudgetPause,
    ) -> None:
        self.config = config
        self.store = store
        self.tracker = tracker
        self.git_host = git_host
        self.llm = llm
        self.audit = audit
        self.pause_if_budget_hit = pause_if_budget_hit

    def run(
        self,
        issue: Issue,
        record: IssueRecord,
        ledger: CostLedger,
        repo_dir: Path,
    ) -> str | None:
        record.transition(IssueState.IMPLEMENTING)
        self.store.upsert(record)
        dry_run = self.config.dry_run
        sandbox = self._sandbox(repo_dir)
        try:
            if not dry_run:
                set_issue_label(self.tracker, self.audit, record, issue, "agent-working")
                sandbox.prepare()
            artifacts = self._test_and_implement(issue, record, ledger, repo_dir, sandbox)
            self._review_loop(issue, record, ledger, repo_dir, sandbox, artifacts)
            self._scan_final_diff(repo_dir)
            if dry_run:
                return self._finish_dry_run(record, ledger, artifacts.all_changes)
            sandbox.close()
            return finalize_draft_pr(
                issue,
                record,
                ledger,
                repo_dir,
                artifacts.verification_commands,
                artifacts.test_output,
                self.git_host,
                self.tracker,
                self.audit,
                self.store,
                _unique_paths(artifacts.all_changes),
            )
        finally:
            if not dry_run:
                sandbox.close()

    def _sandbox(self, repo_dir: Path) -> sandbox_ops.DockerSandbox:
        setup = sandbox_ops.detect_setup_command(repo_dir, self.config.sandbox_setup_command)
        return sandbox_ops.DockerSandbox(
            repo_dir,
            self.config.sandbox_image,
            setup,
            self.config.sandbox_network,
        )

    def _test_and_implement(
        self,
        issue: Issue,
        record: IssueRecord,
        ledger: CostLedger,
        repo_dir: Path,
        sandbox: sandbox_ops.DockerSandbox,
    ) -> WorkArtifacts:
        dry_run = self.config.dry_run
        test_plan = self.llm.write_tests(issue, gather_context(repo_dir, issue))
        ledger.add(test_plan.usage)
        self.pause_if_budget_hit(issue, record, ledger, "test authoring")
        if not test_plan.changes:
            raise RuntimeError("test author returned no changes")
        sandbox_ops.apply_changes(repo_dir, sandbox, test_plan.changes, dry_run)
        baseline_commands = _baseline_test_commands(
            repo_dir,
            test_plan.test_commands,
            self.config.default_test_command,
        )
        baseline = sandbox_ops.run_verification_allow_failure(sandbox, baseline_commands, dry_run)
        plan = self.llm.implement(issue, gather_context(repo_dir, issue))
        ledger.add(plan.usage)
        self.pause_if_budget_hit(issue, record, ledger, "implementation")
        if not plan.changes:
            raise RuntimeError("implementer returned no changes")
        all_changes = [*test_plan.changes, *plan.changes]
        impl_commands = list(plan.test_commands)
        sandbox_ops.ensure_no_secret_commands([*baseline_commands, *impl_commands])
        record.plan = _plan_artifact(
            test_plan.plan,
            baseline,
            plan.plan,
            baseline_commands,
            plan.test_commands,
        )
        self.store.upsert(record)
        sandbox_ops.apply_changes(repo_dir, sandbox, plan.changes, dry_run)
        detected = detect_verification_commands(repo_dir, self.config.default_test_command)
        commands = merge_commands(baseline_commands, impl_commands, detected)
        output = self._run_verification(record, sandbox, commands, dry_run)
        return WorkArtifacts(
            all_changes,
            baseline_commands,
            impl_commands,
            detected,
            commands,
            output,
        )

    def _review_loop(
        self,
        issue: Issue,
        record: IssueRecord,
        ledger: CostLedger,
        repo_dir: Path,
        sandbox: sandbox_ops.DockerSandbox,
        artifacts: WorkArtifacts,
    ) -> None:
        dry_run = self.config.dry_run
        record.transition(IssueState.REVIEW_LOOP)
        self.store.upsert(record)
        panel = ReviewerPanel(self.llm, models=self.config.review_models)
        for round_number in range(1, self.config.max_review_rounds + 1):
            record.review_rounds = round_number
            outcome = panel.review(issue, self._scan_final_diff(repo_dir), ledger)
            record.conversation.setdefault("review_reports", []).append(
                review_round_artifact(round_number, outcome)
            )
            self.store.upsert(record)
            self.pause_if_budget_hit(issue, record, ledger, "review")
            if not outcome.blocking_findings:
                return
            if round_number >= self.config.max_review_rounds:
                raise RuntimeError("review loop stopped with blocking findings")
            fix = self.llm.implement(
                issue,
                gather_context(repo_dir, issue),
                format_blockers(outcome.blocking_findings),
            )
            ledger.add(fix.usage)
            self.pause_if_budget_hit(issue, record, ledger, "review fix")
            if not fix.changes:
                raise RuntimeError("implementer returned no fixes for blocking findings")
            artifacts.all_changes.extend(fix.changes)
            artifacts.impl_commands.extend(fix.test_commands)
            sandbox_ops.apply_changes(repo_dir, sandbox, fix.changes, dry_run)
            commands = merge_commands(
                artifacts.authored_commands,
                artifacts.impl_commands,
                artifacts.detected,
            )
            artifacts.verification_commands = commands
            artifacts.test_output = self._run_verification(record, sandbox, commands, dry_run)

    def _run_verification(
        self,
        record: IssueRecord,
        sandbox: sandbox_ops.DockerSandbox,
        commands: list[str],
        dry_run: bool,
    ) -> str:
        sandbox_ops.ensure_no_secret_commands(commands)
        record.plan["verification_commands"] = commands
        self.store.upsert(record)
        return sandbox_ops.run_verification(sandbox, commands, dry_run)

    def _scan_final_diff(self, repo_dir: Path) -> str:
        diff = self.git_host.current_diff(repo_dir)
        ensure_no_secret_like_values(diff, "diff")
        return diff

    def _finish_dry_run(
        self,
        record: IssueRecord,
        ledger: CostLedger,
        all_changes: list[FileChange],
    ) -> str:
        record.files_touched = _unique_paths(all_changes)
        record.conversation["ci_status"] = {"state": "dry-run"}
        record.conversation["pr_url"] = "dry-run://draft-pr"
        record.pr_url = "dry-run://draft-pr"
        record.transition(IssueState.PR_OPEN)
        record.cost = ledger.to_dict()
        self.store.upsert(record)
        return "dry-run://draft-pr"


def _plan_artifact(
    acceptance_tests: list[str],
    baseline: dict,
    plan: list[str],
    acceptance_test_commands: list[str],
    test_commands: list[str],
) -> dict:
    return {
        "acceptance_tests": acceptance_tests,
        "acceptance_test_baseline": baseline,
        "plan": plan,
        "acceptance_test_commands": acceptance_test_commands,
        "test_author_commands": acceptance_test_commands,
        "test_commands": test_commands,
        "at": utc_now(),
    }


def _baseline_test_commands(
    repo_dir: Path,
    authored_commands: list[str],
    configured: str | None,
) -> list[str]:
    commands = [command.strip() for command in authored_commands if command.strip()]
    if not commands:
        commands = detect_verification_commands(repo_dir, configured).tests
    return list(dict.fromkeys(commands))


def _unique_paths(changes: list[FileChange]) -> list[str]:
    return list(dict.fromkeys(change.path for change in changes))
