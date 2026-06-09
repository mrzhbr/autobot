from __future__ import annotations

from typing import Any

from autobot.models import IssueRecord, IssueState, utc_now
from autobot.scanner import redact_secret_like_values
from autobot.workflow_models import RunLearning, WorkflowConversation


def record_run_learning(record: IssueRecord, message: str) -> None:
    conversation = WorkflowConversation.from_record(record)
    conversation.record_run_learning(extract_run_learning(record, message))
    conversation.save(record)


def extract_run_learning(record: IssueRecord, message: str) -> RunLearning:
    observations = _state_observations(record, message)
    observations.extend(_review_observations(record.conversation.get("review_reports") or []))
    observations.extend(_warning_observations(record.conversation))
    observations.extend(_budget_observations(record.conversation))
    learnings = _learnings(record, observations)
    return RunLearning(
        at=utc_now(),
        state=record.state.value,
        message=redact_secret_like_values(message),
        observations=_unique(observations),
        learnings=_unique(learnings),
        follow_up_actions=_unique(_follow_up_actions(record)),
    )


def _state_observations(record: IssueRecord, message: str) -> list[str]:
    state = record.state
    if state == IssueState.PR_OPEN:
        return [
            f"Draft PR reached after {record.review_rounds} review round(s).",
            f"Touched {len(record.files_touched)} file(s).",
        ]
    if state == IssueState.WAITING:
        blocked = record.blocked_on or message
        return [f"Run paused in waiting state on {redact_secret_like_values(blocked)}."]
    if state == IssueState.ABANDONED:
        blocked = record.blocked_on or message
        return [f"Run abandoned on {redact_secret_like_values(blocked)}."]
    return [f"Run finished in {state.value}: {redact_secret_like_values(message)}."]


def _review_observations(review_reports: list[dict[str, Any]]) -> list[str]:
    observations: list[str] = []
    for item in review_reports:
        blockers = item.get("blocking_findings") or []
        if blockers:
            observations.append(
                f"Review round {item.get('round')} produced {len(blockers)} blocking finding(s)."
            )
    return observations


def _warning_observations(conversation: dict[str, Any]) -> list[str]:
    observations: list[str] = []
    for key in ("audit_warnings", "label_warnings", "finalize_warnings"):
        warnings = conversation.get(key) or []
        if warnings:
            observations.append(f"{key} recorded {len(warnings)} warning(s).")
    return observations


def _budget_observations(conversation: dict[str, Any]) -> list[str]:
    pause = conversation.get("budget_pause")
    if not isinstance(pause, dict):
        return []
    phase = redact_secret_like_values(str(pause.get("phase") or "unknown"))
    return [f"Budget pause was recorded during {phase}."]


def _learnings(record: IssueRecord, observations: list[str]) -> list[str]:
    learnings: list[str] = []
    if record.state == IssueState.PR_OPEN:
        learnings.append("Keep review evidence, touched files, and verification commands together.")
    if record.state == IssueState.WAITING:
        learnings.append("Do not continue the issue until the typed pause reason is resolved.")
    if record.state == IssueState.ABANDONED:
        learnings.append("Preserve the abandoned reason until a human intentionally clears state.")
    if any("blocking finding" in item for item in observations):
        learnings.append("Feed blocking reviewer findings into the next implementation turn.")
    if any("warning(s)" in item for item in observations):
        learnings.append("Retain external side-effect warnings without discarding core progress.")
    if record.cost.get("dollars") is None:
        learnings.append("Configure pricing when dollar-level run accounting is required.")
    return learnings or ["No run-specific issue was detected."]


def _follow_up_actions(record: IssueRecord) -> list[str]:
    actions: list[str] = []
    if record.state == IssueState.PR_OPEN and not record.plan.get("verification_commands"):
        actions.append("Improve verification command capture before relying on the PR.")
    if record.state == IssueState.WAITING and record.blocked_on == "budget":
        actions.append("Increase the budget or reduce model/tool scope before rerun.")
    if record.state == IssueState.ABANDONED:
        actions.append(
            "Inspect blocked_on and clear state only after choosing an intentional retry."
        )
    return actions


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
