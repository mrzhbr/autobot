from __future__ import annotations

import json
import re
import unittest
from datetime import UTC, datetime, timedelta

from autobot.cost import CostLedger
from autobot.models import Issue, IssueRecord
from autobot.pr import build_pr_body


class PrBodyTests(unittest.TestCase):
    def test_body_includes_valid_assumptions_json_and_wall_seconds(self) -> None:
        started_at = (datetime.now(UTC) - timedelta(seconds=3)).isoformat()
        ledger = CostLedger(
            {
                "started_at": started_at,
                "calls": [
                    {
                        "role": "triage",
                        "model": "model",
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "dollars": 0.001,
                    }
                ],
            }
        )
        record = IssueRecord("owner/repo", 1)
        record.plan = {
            "plan": ["Implement the requested behavior."],
            "acceptance_test_baseline": {"ok": False, "output": "red first"},
        }
        record.conversation["human_replies"] = [
            {"author": "alice", "body": "Use a dropdown.", "id": 7}
        ]
        record.review_rounds = 2

        body = build_pr_body(
            Issue("owner/repo", 1, "Add filter", "Body", "alice", []),
            record,
            ledger,
            ["python -m pytest"],
            "tests passed",
            {"state": "success"},
        )

        assumptions = re.search(r"```json\n(.*?)\n```", body, re.S)
        assert assumptions is not None
        self.assertEqual(json.loads(assumptions.group(1))[0]["body"], "Use a dropdown.")
        self.assertIn("- Acceptance test baseline: failed", body)
        self.assertRegex(body, r"- Wall seconds: [0-9]+")
        self.assertIn("- CI status: success", body)

    def test_body_marks_missing_acceptance_baseline_as_not_recorded(self) -> None:
        record = IssueRecord("owner/repo", 1)
        ledger = CostLedger()

        body = build_pr_body(
            Issue("owner/repo", 1, "Add filter", "Body", "alice", []),
            record,
            ledger,
            [],
            "",
            {},
        )

        self.assertIn("- Acceptance test baseline: not recorded", body)

    def test_body_redacts_token_like_values(self) -> None:
        token = "ghp_" + ("A" * 36)
        record = IssueRecord("owner/repo", 1)
        record.plan = {"plan": [f"Use {token}"]}
        record.conversation["human_replies"] = [{"body": token}]

        body = build_pr_body(
            Issue("owner/repo", 1, "Add filter", "Body", "alice", []),
            record,
            CostLedger(),
            [f"printf {token}"],
            token,
            {"state": token},
        )

        self.assertNotIn(token, body)
        self.assertIn("[redacted-secret]", body)

    def test_body_truncates_long_assumption_text_as_valid_json(self) -> None:
        long_reply = "Use a dropdown. " + ("details " * 400)
        record = IssueRecord("owner/repo", 1)
        record.conversation["human_replies"] = [{"author": "alice", "body": long_reply}]

        body = build_pr_body(
            Issue("owner/repo", 1, "Add filter", "Body", "alice", []),
            record,
            CostLedger(),
            [],
            "",
            {"state": "success"},
        )

        assumptions = re.search(r"```json\n(.*?)\n```", body, re.S)
        assert assumptions is not None
        payload = json.loads(assumptions.group(1))
        self.assertLess(len(payload[0]["body"]), len(long_reply))
        self.assertTrue(payload[0]["body"].endswith("...[truncated]"))

    def test_body_fences_backticks_in_assumptions_and_test_output(self) -> None:
        record = IssueRecord("owner/repo", 1)
        record.plan = {"plan": ["Render PR body safely."]}
        record.conversation["human_replies"] = [{"body": "Use ``` in the label."}]

        body = build_pr_body(
            Issue("owner/repo", 1, "Add filter", "Body", "alice", []),
            record,
            CostLedger(),
            ["printf `value`"],
            "before\n```\nafter",
            {"state": "success"},
        )

        self.assertIn("````json", body)
        self.assertIn('"body": "Use ``` in the label."', body)
        self.assertIn("- `` printf `value` ``", body)
        self.assertIn("````text\nbefore\n```\nafter\n````", body)


if __name__ == "__main__":
    unittest.main()
