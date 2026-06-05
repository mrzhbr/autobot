from __future__ import annotations

from dataclasses import dataclass

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
    def __init__(self, llm: LLM, lenses: list[str] | None = None) -> None:
        self.llm = llm
        self.lenses = lenses or REVIEW_LENSES

    def review(self, issue: Issue, diff: str, ledger: CostLedger) -> ReviewOutcome:
        reports: list[ReviewReport] = []
        blockers: list[ReviewFinding] = []
        for lens in self.lenses:
            report = self.llm.review(lens, issue, diff)
            ledger.add(report.usage)
            reports.append(report)
            blockers.extend(finding for finding in report.findings if finding.blocking)
        return ReviewOutcome(reports=reports, blocking_findings=blockers)


def format_blockers(findings: list[ReviewFinding]) -> list[str]:
    lines: list[str] = []
    for finding in findings:
        location = finding.file
        if finding.line is not None:
            location += f":{finding.line}"
        lines.append(f"[{finding.severity}] {location} {finding.message}".strip())
    return lines
