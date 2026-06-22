from __future__ import annotations

import unittest

from pydantic import ValidationError

from autobot.schemas import ImplementationPayload, PlannerPayload, ReviewPayload, TriagePayload


class SchemaTests(unittest.TestCase):
    def test_triage_requires_question_when_not_ready(self) -> None:
        with self.assertRaises(ValidationError):
            TriagePayload.model_validate(
                {"ready": False, "questions": [], "reason": "Missing choice."}
            )

    def test_triage_trims_questions_and_caps_count(self) -> None:
        payload = TriagePayload.model_validate(
            {
                "ready": False,
                "questions": [
                    "  Pick dropdown or radio?  ",
                    "How should empty results look?",
                    "Should the choice persist?",
                    "Which color should it be?",
                ],
                "reason": "Choice.",
            }
        )

        self.assertEqual(
            payload.questions,
            [
                "Pick dropdown or radio?",
                "How should empty results look?",
                "Should the choice persist?",
            ],
        )

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

    def test_implementation_rejects_empty_plan_after_trimming(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            ImplementationPayload.model_validate(
                {
                    "plan": ["   "],
                    "changes": [{"path": "README.md", "content": "# Demo\n"}],
                    "test_commands": [],
                }
            )

        self.assertIn("implementation plan must include", str(raised.exception))

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

    def test_planner_contract_requires_version(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            PlannerPayload.model_validate(
                {
                    "summary": "Patch router confidence handling.",
                    "target_files": ["src/router.py"],
                    "constraints": [],
                    "implementation_steps": ["  "],
                    "tests_to_add": [],
                    "verification_commands": [],
                    "risks": [],
                    "non_goals": [],
                }
            )

        self.assertIn("contract_version", str(raised.exception))

    def test_planner_contract_requires_actionable_steps(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            PlannerPayload.model_validate(
                {
                    "contract_version": 1,
                    "summary": "Patch router confidence handling.",
                    "target_files": ["src/router.py"],
                    "constraints": [],
                    "implementation_steps": ["  "],
                    "tests_to_add": [],
                    "verification_commands": [],
                    "risks": [],
                    "non_goals": [],
                }
            )

        self.assertIn("planner contract must include", str(raised.exception))

    def test_planner_contract_trims_fields(self) -> None:
        payload = PlannerPayload.model_validate(
            {
                "contract_version": 1,
                "summary": " Patch router confidence handling. ",
                "target_files": [" src/router.py ", ""],
                "constraints": [" Keep public API stable. "],
                "implementation_steps": [" Read RouterResult. "],
                "tests_to_add": [" Low-confidence route test. "],
                "verification_commands": [" python -m pytest tests/test_router.py "],
                "risks": [" Ambiguous router names. "],
                "non_goals": [" No model provider changes. "],
            }
        )

        self.assertEqual(payload.summary, "Patch router confidence handling.")
        self.assertEqual(payload.target_files, ["src/router.py"])
        self.assertEqual(payload.implementation_steps, ["Read RouterResult."])

    def test_review_payload_accepts_empty_findings(self) -> None:
        payload = ReviewPayload.model_validate({"findings": []})

        self.assertEqual(payload.findings, [])

    def test_review_payload_normalizes_common_severity_variants(self) -> None:
        payload = ReviewPayload.model_validate(
            {
                "findings": [
                    {
                        "severity": "notice",
                        "file": "src/demo.py",
                        "line": 1,
                        "message": "Document this edge case.",
                        "blocking": False,
                    },
                    {
                        "severity": "Minor",
                        "file": "src/demo.py",
                        "line": 2,
                        "message": "Name could be clearer.",
                        "blocking": False,
                    },
                    {
                        "severity": "major",
                        "file": "src/demo.py",
                        "line": 3,
                        "message": "Incorrect behavior.",
                        "blocking": True,
                    },
                    {
                        "severity": "blocker",
                        "file": "src/demo.py",
                        "line": 4,
                        "message": "Cannot ship.",
                        "blocking": True,
                    },
                ]
            }
        )

        self.assertEqual(
            [finding.severity for finding in payload.findings],
            ["info", "low", "high", "critical"],
        )

    def test_review_payload_canonicalizes_unexpected_severity_from_blocking_flag(self) -> None:
        payload = ReviewPayload.model_validate(
            {
                "findings": [
                    {
                        "severity": "needs attention",
                        "file": "src/demo.py",
                        "line": 1,
                        "message": "Unknown blocking severity should be high.",
                        "blocking": True,
                    },
                    {
                        "severity": "cosmetic cleanup",
                        "file": "src/demo.py",
                        "line": 2,
                        "message": "Unknown non-blocking severity should be low.",
                        "blocking": False,
                    },
                    {
                        "severity": "catastrophic",
                        "file": "src/demo.py",
                        "line": 3,
                        "message": "Recognized severe aliases stay critical.",
                        "blocking": True,
                    },
                ]
            }
        )

        self.assertEqual(
            [finding.severity for finding in payload.findings],
            ["high", "low", "critical"],
        )


if __name__ == "__main__":
    unittest.main()
