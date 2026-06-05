from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autobot.models import IssueRecord, IssueState
from autobot.state import StateStore


class StateStoreTests(unittest.TestCase):
    def test_persists_issue_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            record = IssueRecord(repo="owner/repo", issue_number=7)
            record.transition(IssueState.WAITING)
            record.branch = "autobot/issue-7-demo"
            record.conversation["asked_comment_id"] = 123
            store.upsert(record)

            loaded = store.get("owner/repo", 7)

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.state, IssueState.WAITING)
            self.assertEqual(loaded.branch, "autobot/issue-7-demo")
            self.assertEqual(loaded.conversation["asked_comment_id"], 123)


if __name__ == "__main__":
    unittest.main()
