from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autobot.audit import AuditLog


class AuditLogTests(unittest.TestCase):
    def test_records_jsonl_rows_with_expected_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            AuditLog(path).record("push", "owner/repo", 1, {"branch": "autobot/issue-1"})

            row = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(row["action"], "push")
        self.assertEqual(row["repo"], "owner/repo")
        self.assertEqual(row["issue_number"], 1)
        self.assertEqual(row["details"], {"branch": "autobot/issue-1"})
        self.assertIn("at", row)

    def test_redacts_sensitive_detail_keys_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            AuditLog(path).record(
                "draft_pr",
                "owner/repo",
                1,
                {
                    "token": "ghp_secret",
                    "api-key": "sk-secret",
                    "nested": [{"password": "pw"}, {"authorization": "bearer token"}],
                    "branch": "autobot/issue-1",
                },
            )

            text = path.read_text(encoding="utf-8")
            row = json.loads(text)

        self.assertNotIn("ghp_secret", text)
        self.assertNotIn("sk-secret", text)
        self.assertEqual(row["details"]["token"], "[redacted]")
        self.assertEqual(row["details"]["api-key"], "[redacted]")
        self.assertEqual(row["details"]["nested"][0]["password"], "[redacted]")
        self.assertEqual(row["details"]["nested"][1]["authorization"], "[redacted]")
        self.assertEqual(row["details"]["branch"], "autobot/issue-1")

    def test_redacts_token_like_string_values_without_sensitive_keys(self) -> None:
        token = "ghp_" + ("A" * 36)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            AuditLog(path).record("comment", "owner/repo", 1, {"body": f"failed {token}"})

            text = path.read_text(encoding="utf-8")
            row = json.loads(text)

        self.assertNotIn(token, text)
        self.assertEqual(row["details"]["body"], "failed [redacted-secret]")

    def test_truncates_large_string_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            AuditLog(path).record("comment", "owner/repo", 1, {"body": "x" * 1300})

            row = json.loads(path.read_text(encoding="utf-8"))

        self.assertLess(len(row["details"]["body"]), 1300)
        self.assertTrue(row["details"]["body"].endswith("...[truncated]"))


if __name__ == "__main__":
    unittest.main()
