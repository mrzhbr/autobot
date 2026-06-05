from __future__ import annotations

from pathlib import Path

from autobot.adapters import GitHost, IssueTracker
from autobot.audit import AuditLog, record_best_effort
from autobot.cost import CostLedger
from autobot.labels import set_issue_label
from autobot.models import Issue, IssueRecord, IssueState, utc_now
from autobot.pr import build_pr_body
from autobot.scanner import redact_secret_like_values
from autobot.state import StateStore
from autobot.workspace import branch_name, changed_files


def finalize_draft_pr(
    issue: Issue,
    record: IssueRecord,
    ledger: CostLedger,
    repo_dir: Path,
    verification_commands: list[str],
    test_output: str,
    git_host: GitHost,
    tracker: IssueTracker,
    audit: AuditLog,
    store: StateStore,
    files_to_commit: list[str] | None = None,
) -> str:
    committed = git_host.commit_all(
        repo_dir, f"feat: implement issue #{issue.number}", files_to_commit
    )
    if not committed:
        raise RuntimeError("no changes to commit")
    branch = record.branch or branch_name(issue)
    git_host.push(issue.repo, repo_dir, branch)
    record_best_effort(audit, "push", issue.repo, issue.number, {"branch": branch}, record)
    ci_status = git_host.ci_status(issue.repo, branch)
    record.conversation["ci_status"] = ci_status
    pr_url = git_host.open_draft_pr(
        issue.repo,
        branch,
        f"Draft: {issue.title}",
        build_pr_body(issue, record, ledger, verification_commands, test_output, ci_status),
    )
    record.conversation["pr_url"] = pr_url
    record.pr_url = pr_url
    store.upsert(record)
    record_best_effort(
        audit,
        "draft_pr",
        issue.repo,
        issue.number,
        {"url": pr_url, "branch": branch},
        record,
    )
    set_issue_label(tracker, audit, record, issue, "agent-pr-open")
    _record_changed_files(record, repo_dir)
    record.transition(IssueState.PR_OPEN)
    store.upsert(record)
    return pr_url


def _record_changed_files(record: IssueRecord, repo_dir: Path) -> None:
    try:
        record.files_touched = changed_files(repo_dir)
    except Exception as exc:
        record.conversation.setdefault("finalize_warnings", []).append(
            {
                "action": "changed_files",
                "error": redact_secret_like_values(str(exc)),
                "at": utc_now(),
            }
        )
