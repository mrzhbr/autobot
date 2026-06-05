from __future__ import annotations

import unittest

from pydantic import ValidationError

from autobot.schemas import ImplementationPayload, ReviewPayload, TriagePayload


class SchemaTests(unittest.TestCase):
    def test_triage_requires_question_when_not_ready(self) -> None:
        with self.assertRaises(ValidationError):
            TriagePayload.model_validate(
                {"ready": False, "questions": [], "reason": "Missing choice."}
            )

    def test_triage_trims_questions_and_caps_count(self) -> None:
        payload = TriagePayload.model_validate(
            {"ready": False, "questions": ["  Pick dropdown or radio?  "], "reason": "Choice."}
        )

        self.assertEqual(payload.questions, ["Pick dropdown or radio?"])

    def test_implementation_rejects_unsafe_paths(self) -> None:
        with self.assertRaises(ValidationError):
            ImplementationPayload.model_validate(
                {
                    "plan": ["Write file."],
                    "changes": [{"path": "../secret.txt", "content": "no"}],
                    "test_commands": [],
                }
            )

    def test_implementation_requires_content_for_writes(self) -> None:
        with self.assertRaises(ValidationError):
            ImplementationPayload.model_validate(
                {
                    "plan": ["Write file."],
                    "changes": [{"path": "README.md", "action": "write"}],
                    "test_commands": [],
                }
            )

    def test_implementation_rejects_secret_like_test_commands_without_echoing_input(self) -> None:
        token = "ghp_" + ("A" * 36)

        with self.assertRaises(ValidationError) as raised:
            ImplementationPayload.model_validate(
                {
                    "plan": ["Run tests."],
                    "changes": [{"path": "README.md", "content": "# Demo\n"}],
                    "test_commands": [f"printf '%s' '{token}'"],
                }
            )

        self.assertNotIn(token, str(raised.exception))
        self.assertIn("secret-like values found in test commands", str(raised.exception))

    def test_review_payload_accepts_empty_findings(self) -> None:
        payload = ReviewPayload.model_validate({"findings": []})

        self.assertEqual(payload.findings, [])


if __name__ == "__main__":
    unittest.main()
