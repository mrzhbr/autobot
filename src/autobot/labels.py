from __future__ import annotations

from autobot.adapters import IssueTracker
from autobot.audit import AuditLog
from autobot.models import Issue, IssueRecord, utc_now
from autobot.scanner import redact_secret_like_values


def set_issue_label(
    tracker: IssueTracker,
    audit: AuditLog,
    record: IssueRecord,
    issue: Issue,
    label: str,
) -> None:
    try:
        tracker.set_label(issue.repo, issue.number, label)
    except Exception as exc:
        error = redact_secret_like_values(str(exc))
        record.conversation.setdefault("label_warnings", []).append(
            {"label": label, "error": error, "at": utc_now()}
        )
        audit.record("label_failed", issue.repo, issue.number, {"label": label, "error": error})
        return
    audit.record("label", issue.repo, issue.number, {"label": label})
