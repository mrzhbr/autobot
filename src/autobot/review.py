from __future__ import annotations

from dataclasses import asdict, dataclass

from autobot.adapters import LLM
from autobot.cost import CostLedger
from autobot.models import Issue, ReviewFinding, ReviewReport

REVIEW_LENSES = [
    "correctness and acceptance criteria",
    "security and secret handling",
    "project conventions and maintainability",
    "test quality and regression coverage",
]


@dataclass(frozen=True)
class ReviewOutcome:
    reports: list[ReviewReport]
    blocking_findings: list[ReviewFinding]


class ReviewerPanel:
    def __init__(
        self,
        llm: LLM,
        lenses: list[str] | None = None,
        models: list[str] | None = None,
    ) -> None:
        self.llm = llm
        self.lenses = lenses or REVIEW_LENSES
        self.models = models or []

    def review(self, issue: Issue, diff: str, ledger: CostLedger) -> ReviewOutcome:
        reports: list[ReviewReport] = []
        blockers: list[ReviewFinding] = []
        for index, lens in enumerate(self.lenses):
            model = self._model_for(index)
            report = self.llm.review(lens, issue, diff, model=model)
            ledger.add(report.usage)
            reports.append(report)
            blockers.extend(finding for finding in report.findings if finding.blocking)
        return ReviewOutcome(reports=reports, blocking_findings=blockers)

    def _model_for(self, index: int) -> str | None:
        if not self.models:
            return None
        return self.models[index % len(self.models)]


def format_blockers(findings: list[ReviewFinding]) -> list[str]:
    lines: list[str] = []
    for finding in findings:
        location = finding.file
        if finding.line is not None:
            location += f":{finding.line}"
        lines.append(f"[{finding.severity}] {location} {finding.message}".strip())
    return lines


def review_round_artifact(round_number: int, outcome: ReviewOutcome) -> dict:
    return {
        "round": round_number,
        "reports": [asdict(report) for report in outcome.reports],
        "blocking_findings": [asdict(finding) for finding in outcome.blocking_findings],
    }
