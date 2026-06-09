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
from autobot.harness import (
    HarnessResult,
    HarnessSession,
    HarnessTask,
    HarnessTaskKind,
    PlanningResult,
    build_harness_adapter,
)
from autobot.labels import set_issue_label
from autobot.models import FileChange, Issue, IssueRecord, IssueState, utc_now
from autobot.pi_harness import pi_container_env_names
from autobot.pr_flow import finalize_draft_pr
from autobot.review import ReviewerPanel, format_blockers, review_round_artifact
from autobot.scanner import ensure_no_secret_like_values
from autobot.state import StateStore
from autobot.tests import VerificationCommands, detect_verification_commands
from autobot.tests import merge_verification_commands as merge_commands
from autobot.workflow_models import WorkflowConversation

BudgetPause = Callable[[Issue, IssueRecord, CostLedger, str], None]


@dataclass
class WorkArtifacts:
    all_changes: list[FileChange]
    authored_commands: list[str]
    impl_commands: list[str]
    detected: VerificationCommands
    verification_commands: list[str]
    test_output: str
    changed_paths: list[str]
    planner: PlanningResult | None = None


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
        harness = None
        try:
            if not dry_run:
                set_issue_label(self.tracker, self.audit, record, issue, "agent-working")
                sandbox.prepare()
            harness = build_harness_adapter(self.config, self.llm).start(
                repo_dir,
                sandbox=sandbox,
                dry_run=dry_run,
            )
            planner = self._run_planner(issue, record, ledger, repo_dir, sandbox, harness)
            artifacts = self._test_and_implement(
                issue,
                record,
                ledger,
                repo_dir,
                sandbox,
                harness,
                planner,
            )
            self._review_loop(issue, record, ledger, repo_dir, sandbox, harness, artifacts)
            self._scan_final_diff(repo_dir, artifacts.changed_paths)
            if dry_run:
                return self._finish_dry_run(record, ledger, artifacts.changed_paths)
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
                artifacts.changed_paths,
            )
        finally:
            if harness is not None:
                harness.close()
            if not dry_run:
                sandbox.close()

    def _sandbox(self, repo_dir: Path) -> sandbox_ops.DockerSandbox:
        setup = sandbox_ops.detect_setup_command(repo_dir, self.config.sandbox_setup_command)
        return sandbox_ops.DockerSandbox(
            repo_dir,
            self.config.sandbox_image,
            setup,
            self.config.sandbox_network,
            env_names=pi_container_env_names(self.config),
            mode="copy" if self.config.sandbox_backend == "docker-copy" else "bind",
        )

    def _run_planner(
        self,
        issue: Issue,
        record: IssueRecord,
        ledger: CostLedger,
        repo_dir: Path,
        sandbox: sandbox_ops.DockerSandbox,
        harness: HarnessSession,
    ) -> PlanningResult | None:
        if not self.config.planner_enabled:
            return None
        if self.config.dry_run or self.config.mock_llm:
            planner = harness
        elif self.config.planner_harness == "pi":
            from autobot.pi_harness import PiHarnessAdapter

            planner = PiHarnessAdapter(self.config).start_planner(repo_dir, sandbox)
        else:
            raise RuntimeError(f"unknown planner harness: {self.config.planner_harness}")
        try:
            result = planner.plan(
                HarnessTask(HarnessTaskKind.PLANNING, issue, gather_context(repo_dir, issue))
            )
        finally:
            if planner is not harness:
                planner.close()
        ledger.add(result.usage)
        self.pause_if_budget_hit(issue, record, ledger, "planning")
        record.plan["planner"] = result.model_dump()
        self.store.upsert(record)
        return result

    def _test_and_implement(
        self,
        issue: Issue,
        record: IssueRecord,
        ledger: CostLedger,
        repo_dir: Path,
        sandbox: sandbox_ops.DockerSandbox,
        harness: HarnessSession,
        planner: PlanningResult | None,
    ) -> WorkArtifacts:
        dry_run = self.config.dry_run
        planning_context = planner.as_prompt_context() if planner is not None else None
        test_result = harness.run(
            HarnessTask(
                HarnessTaskKind.TEST_AUTHOR,
                issue,
                gather_context(repo_dir, issue),
                planning_context=planning_context,
            )
        )
        ledger.add(test_result.usage)
        self.pause_if_budget_hit(issue, record, ledger, "test authoring")
        if not _result_paths(test_result):
            raise RuntimeError("test author returned no changes")
        _apply_harness_result(repo_dir, sandbox, test_result, dry_run)
        baseline_commands = _baseline_test_commands(
            repo_dir,
            test_result.test_commands,
            self.config.default_test_command,
        )
        baseline = sandbox_ops.run_verification_allow_failure(sandbox, baseline_commands, dry_run)
        plan_result = harness.run(
            HarnessTask(
                HarnessTaskKind.IMPLEMENT,
                issue,
                gather_context(repo_dir, issue),
                planning_context=planning_context,
            )
        )
        ledger.add(plan_result.usage)
        self.pause_if_budget_hit(issue, record, ledger, "implementation")
        if not _result_paths(plan_result):
            raise RuntimeError("implementer returned no changes")
        all_changes = [*test_result.changes, *plan_result.changes]
        changed_paths = _unique_strings([*_result_paths(test_result), *_result_paths(plan_result)])
        impl_commands = list(plan_result.test_commands)
        sandbox_ops.ensure_no_secret_commands([*baseline_commands, *impl_commands])
        record.plan = _plan_artifact(
            test_result.plan,
            baseline,
            plan_result.plan,
            baseline_commands,
            plan_result.test_commands,
        )
        if planner is not None:
            record.plan["planner"] = planner.model_dump()
        self.store.upsert(record)
        _apply_harness_result(repo_dir, sandbox, plan_result, dry_run)
        detected = detect_verification_commands(repo_dir, self.config.default_test_command)
        commands = merge_commands(baseline_commands, impl_commands, detected)
        artifacts = WorkArtifacts(
            all_changes,
            baseline_commands,
            impl_commands,
            detected,
            commands,
            "",
            changed_paths,
            planner,
        )
        self._run_verification_with_fixes(
            issue,
            record,
            ledger,
            repo_dir,
            sandbox,
            harness,
            artifacts,
            dry_run,
        )
        return artifacts

    def _review_loop(
        self,
        issue: Issue,
        record: IssueRecord,
        ledger: CostLedger,
        repo_dir: Path,
        sandbox: sandbox_ops.DockerSandbox,
        harness: HarnessSession,
        artifacts: WorkArtifacts,
    ) -> None:
        dry_run = self.config.dry_run
        record.transition(IssueState.REVIEW_LOOP)
        self.store.upsert(record)
        panel = ReviewerPanel(self.llm, models=self.config.review_models)
        for round_number in range(1, self.config.max_review_rounds + 1):
            record.review_rounds = round_number
            outcome = panel.review(
                issue,
                self._review_input(repo_dir, artifacts),
                ledger,
            )
            conversation = WorkflowConversation.from_record(record)
            conversation.record_review_round(review_round_artifact(round_number, outcome))
            conversation.save(record)
            self.store.upsert(record)
            self.pause_if_budget_hit(issue, record, ledger, "review")
            if not outcome.blocking_findings:
                return
            if round_number >= self.config.max_review_rounds:
                raise RuntimeError("review loop stopped with blocking findings")
            fix = harness.run(
                HarnessTask(
                    HarnessTaskKind.REVIEW_FIX,
                    issue,
                    gather_context(repo_dir, issue),
                    format_blockers(outcome.blocking_findings),
                    planning_context=(
                        artifacts.planner.as_prompt_context()
                        if artifacts.planner is not None
                        else None
                    ),
                )
            )
            ledger.add(fix.usage)
            self.pause_if_budget_hit(issue, record, ledger, "review fix")
            if not _result_paths(fix):
                raise RuntimeError("implementer returned no fixes for blocking findings")
            artifacts.all_changes.extend(fix.changes)
            artifacts.impl_commands.extend(fix.test_commands)
            artifacts.changed_paths = _unique_strings(
                [*artifacts.changed_paths, *_result_paths(fix)]
            )
            _apply_harness_result(repo_dir, sandbox, fix, dry_run)
            commands = merge_commands(
                artifacts.authored_commands,
                artifacts.impl_commands,
                artifacts.detected,
            )
            artifacts.verification_commands = commands
            self._run_verification_with_fixes(
                issue,
                record,
                ledger,
                repo_dir,
                sandbox,
                harness,
                artifacts,
                dry_run,
            )

    def _run_verification_with_fixes(
        self,
        issue: Issue,
        record: IssueRecord,
        ledger: CostLedger,
        repo_dir: Path,
        sandbox: sandbox_ops.DockerSandbox,
        harness: HarnessSession,
        artifacts: WorkArtifacts,
        dry_run: bool,
    ) -> None:
        for attempt in range(1, self.config.max_review_rounds + 1):
            try:
                artifacts.test_output = self._run_verification(
                    record,
                    sandbox,
                    artifacts.verification_commands,
                    dry_run,
                )
                return
            except sandbox_ops.SandboxError as exc:
                if attempt >= self.config.max_review_rounds:
                    raise
                fix = harness.run(
                    HarnessTask(
                        HarnessTaskKind.VERIFICATION_FIX,
                        issue,
                        gather_context(repo_dir, issue),
                        [_verification_failure_blocker(exc)],
                        planning_context=(
                            artifacts.planner.as_prompt_context()
                            if artifacts.planner is not None
                            else None
                        ),
                    )
                )
                ledger.add(fix.usage)
                self.pause_if_budget_hit(issue, record, ledger, "verification fix")
                if not _result_paths(fix):
                    raise RuntimeError(
                        "implementer returned no fixes for verification failure"
                    ) from exc
                artifacts.authored_commands = _drop_failed_commands(
                    artifacts.authored_commands, exc
                )
                artifacts.impl_commands = _drop_failed_commands(artifacts.impl_commands, exc)
                artifacts.all_changes.extend(fix.changes)
                artifacts.impl_commands.extend(fix.test_commands)
                artifacts.changed_paths = _unique_strings(
                    [*artifacts.changed_paths, *_result_paths(fix)]
                )
                _apply_harness_result(repo_dir, sandbox, fix, dry_run)
                artifacts.verification_commands = merge_commands(
                    artifacts.authored_commands,
                    artifacts.impl_commands,
                    artifacts.detected,
                )
        raise RuntimeError("verification loop exhausted unexpectedly")

    def _run_verification(
        self,
        record: IssueRecord,
        sandbox: sandbox_ops.DockerSandbox,
        commands: list[str],
        dry_run: bool,
    ) -> str:
        commands = sandbox_ops.normalize_verification_commands(commands)
        sandbox_ops.ensure_no_secret_commands(commands)
        record.plan["verification_commands"] = commands
        self.store.upsert(record)
        return sandbox_ops.run_verification(sandbox, commands, dry_run)

    def _scan_final_diff(self, repo_dir: Path, paths: list[str]) -> str:
        diff = self.git_host.current_diff(repo_dir, paths)
        ensure_no_secret_like_values(diff, "diff")
        return diff

    def _review_input(self, repo_dir: Path, artifacts: WorkArtifacts) -> str:
        diff = self._scan_final_diff(repo_dir, artifacts.changed_paths)
        if artifacts.planner is None:
            return diff
        return diff + "\n\nPlanner artifact:\n" + artifacts.planner.as_prompt_context()

    def _finish_dry_run(
        self,
        record: IssueRecord,
        ledger: CostLedger,
        changed_paths: list[str],
    ) -> str:
        record.files_touched = changed_paths
        conversation = WorkflowConversation.from_record(record)
        conversation.record_pr_open("dry-run://draft-pr", {"state": "dry-run"})
        conversation.save(record)
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
    return _unique_strings([change.path for change in changes])


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _result_paths(result: HarnessResult) -> list[str]:
    return result.changed_paths or _unique_paths(result.changes)


def _verification_failure_blocker(exc: Exception) -> str:
    message = str(exc)
    if len(message) > 4000:
        message = message[:4000] + "...[truncated]"
    return (
        "[high] Verification failed. Fix the implementation so every verification "
        f"command passes before review.\n{message}"
    )


def _drop_failed_commands(commands: list[str], exc: Exception) -> list[str]:
    message = str(exc)
    return [command for command in commands if f"$ {command}\n" not in message]


def _apply_harness_result(
    repo_dir: Path,
    sandbox: sandbox_ops.DockerSandbox,
    result: HarnessResult,
    dry_run: bool,
) -> None:
    if not result.applied_in_workspace:
        sandbox_ops.apply_changes(repo_dir, sandbox, result.changes, dry_run)
    if not dry_run:
        sandbox.sync_to_host(_result_paths(result))
