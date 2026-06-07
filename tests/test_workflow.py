from __future__ import annotations

import unittest

from pydantic import ValidationError

from autobot.models import IssueRecord, IssueState, TriageDecision
from autobot.workflow_models import (
    ContinueStep,
    HumanReply,
    WorkflowConversation,
    WorkflowStep,
    validate_pr_open_evidence,
    validate_step_result,
    validate_workflow_transition,
)


class WorkflowTests(unittest.TestCase):
    def test_step_result_validates_discriminated_continue(self) -> None:
        result = validate_step_result(
            {"kind": "continue", "next_step": WorkflowStep.TERMINAL_CHECK.value}
        )

        self.assertIsInstance(result, ContinueStep)
        self.assertEqual(result.next_step, WorkflowStep.TERMINAL_CHECK)

    def test_wait_step_requires_typed_pause_kind(self) -> None:
        with self.assertRaises(ValidationError):
            validate_step_result({"kind": "wait", "pause": "unknown", "message": "blocked"})

    def test_invalid_workflow_transition_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid workflow transition"):
            validate_workflow_transition(WorkflowStep.LOAD_RECORD, WorkflowStep.READ_ISSUE)

    def test_conversation_round_trips_existing_json_shape(self) -> None:
        record = IssueRecord(repo="owner/repo", issue_number=1)
        conversation = WorkflowConversation.from_record(record)
        conversation.record_triage(TriageDecision(False, ["Which mode?"], "Missing choice."))
        conversation.record_clarification_pause(7, ["Which mode?"])
        conversation.record_human_replies(
            [HumanReply(id=8, author="alice", body="Use compact.", created_at="now")]
        )
        conversation.save(record)

        self.assertEqual(record.conversation["asked_comment_id"], 7)
        self.assertEqual(record.conversation["asked_questions"], ["Which mode?"])
        self.assertEqual(record.conversation["human_replies"][0]["author"], "alice")
        loaded = WorkflowConversation.from_record(record)
        self.assertEqual(loaded.resume_marker(), 7)

    def test_conversation_rejects_unknown_keys(self) -> None:
        record = IssueRecord(
            repo="owner/repo",
            issue_number=1,
            conversation={"unexpected": "value"},
        )

        with self.assertRaises(ValidationError):
            WorkflowConversation.from_record(record)

    def test_pr_open_requires_url_review_and_file_evidence(self) -> None:
        record = IssueRecord(repo="owner/repo", issue_number=1, state=IssueState.PR_OPEN)

        with self.assertRaisesRegex(ValueError, "draft PR URL"):
            validate_pr_open_evidence(record)

        conversation = WorkflowConversation.from_record(record)
        conversation.record_pr_open("https://github.test/pull/1", {"state": "success"})
        conversation.save(record)
        record.pr_url = "https://github.test/pull/1"

        with self.assertRaisesRegex(ValueError, "review round"):
            validate_pr_open_evidence(record)

        record.review_rounds = 1
        with self.assertRaisesRegex(ValueError, "touched-file"):
            validate_pr_open_evidence(record)

        record.files_touched = ["README.md"]
        validate_pr_open_evidence(record)
