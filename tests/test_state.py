from __future__ import annotations

import sqlite3
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

    def test_persists_pr_url_in_state_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            record = IssueRecord(repo="owner/repo", issue_number=7)
            record.pr_url = "https://github.test/pull/7"
            store.upsert(record)

            loaded = store.get("owner/repo", 7)

            assert loaded is not None
            self.assertEqual(loaded.pr_url, "https://github.test/pull/7")

    def test_migrates_pr_url_from_legacy_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    """
                    create table issue_state (
                        repo text not null,
                        issue_number integer not null,
                        state text not null,
                        conversation_json text not null,
                        branch text,
                        plan_json text not null,
                        cost_json text not null,
                        blocked_on text,
                        review_rounds integer not null,
                        files_touched_json text not null,
                        created_at text not null,
                        updated_at text not null,
                        primary key (repo, issue_number)
                    )
                    """
                )
                conn.execute(
                    """
                    insert into issue_state values (
                        'owner/repo', 7, 'pr_open',
                        '{"pr_url": "https://github.test/pull/7"}',
                        'autobot/issue-7', '{}', '{}', null, 0, '[]',
                        '2026-06-05T00:00:00+00:00',
                        '2026-06-05T00:00:00+00:00'
                    )
                    """
                )

            loaded = StateStore(path).get("owner/repo", 7)

            assert loaded is not None
            self.assertEqual(loaded.pr_url, "https://github.test/pull/7")


if __name__ == "__main__":
    unittest.main()
