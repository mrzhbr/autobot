from __future__ import annotations

import unittest

from autobot.cost import CostLedger
from autobot.models import Issue, ReviewReport, Usage
from autobot.review import ReviewerPanel


class RecordingLLM:
    def __init__(self) -> None:
        self.models: list[str | None] = []

    def review(
        self,
        lens: str,
        issue: Issue,
        diff: str,
        model: str | None = None,
    ) -> ReviewReport:
        self.models.append(model)
        return ReviewReport(lens, [], Usage("review", model or "fallback", 1, 1, 0.001))


class ReviewerPanelTests(unittest.TestCase):
    def test_rotates_configured_review_models_across_lenses(self) -> None:
        llm = RecordingLLM()
        panel = ReviewerPanel(
            llm,
            lenses=["correctness", "security", "style", "tests"],
            models=["model-a", "model-b"],
        )
        issue = Issue("owner/repo", 1, "Title", "Body", "alice", [])
        ledger = CostLedger()

        outcome = panel.review(issue, "diff", ledger)

        self.assertEqual(llm.models, ["model-a", "model-b", "model-a", "model-b"])
        self.assertEqual(len(outcome.reports), 4)
        self.assertEqual(ledger.total_tokens, 8)


if __name__ == "__main__":
    unittest.main()
