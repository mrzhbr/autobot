from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from autobot.cost import CostLedger
from autobot.learning import extract_run_learning
from autobot.models import IssueRecord, IssueState
from autobot.result import finish_process
from autobot.state import StateStore


class LearningTests(unittest.TestCase):
    def test_extracts_pr_open_learning_from_review_and_warnings(self) -> None:
        record = IssueRecord(
            repo="owner/repo",
            issue_number=7,
            state=IssueState.PR_OPEN,
            review_rounds=2,
            files_touched=["src/app.py"],
            plan={"verification_commands": ["python -m unittest"]},
            cost={"dollars": 0.12},
            conversation={
                "review_reports": [
                    {"round": 1, "blocking_findings": [{"message": "fix this"}]},
                    {"round": 2, "blocking_findings": []},
                ],
                "label_warnings": [{"label": "agent-working", "error": "missing"}],
                "pr_url": "https://github.test/pull/1",
            },
            pr_url="https://github.test/pull/1",
        )

        learning = extract_run_learning(record, "opened draft pull request")

        self.assertEqual(learning.state, "pr_open")
        self.assertIn("Review round 1 produced 1 blocking finding(s).", learning.observations)
        self.assertIn("label_warnings recorded 1 warning(s).", learning.observations)
        self.assertIn(
            "Feed blocking reviewer findings into the next implementation turn.",
            learning.learnings,
        )
        self.assertIn(
            "Retain external side-effect warnings without discarding core progress.",
            learning.learnings,
        )

    def test_finish_process_persists_run_learning(self) -> None:
        with TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            record = store.ensure("owner/repo", 3)
            record.transition(IssueState.WAITING)
            record.blocked_on = "clarification"
            ledger = CostLedger(record.cost)

            result = finish_process(store, record, ledger, "waiting for human reply", None, 0.0)

            self.assertEqual(result.state, IssueState.WAITING)
            loaded = store.get("owner/repo", 3)
            self.assertIsNotNone(loaded)
            learnings = loaded.conversation["run_learnings"]
            self.assertEqual(learnings[0]["state"], "waiting")
            self.assertIn("typed pause reason", learnings[0]["learnings"][0])


if __name__ == "__main__":
    unittest.main()
