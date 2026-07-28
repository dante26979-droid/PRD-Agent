from __future__ import annotations

from dataclasses import dataclass
import time

from .models import ExternalInvestigationRequest


@dataclass(frozen=True)
class InvestigatorEvalCase:
    case_id: str
    request: ExternalInvestigationRequest
    expected_locators: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True)
class InvestigatorProfileMetrics:
    case_count: int
    evidence_count: int
    true_positive_count: int
    expected_count: int
    evidence_precision: float
    evidence_recall: float
    duration_ms: int
    rejected_candidates: int


@dataclass(frozen=True)
class InvestigatorAblationReport:
    case_ids: tuple[str, ...]
    native: InvestigatorProfileMetrics
    external: InvestigatorProfileMetrics


class ExternalInvestigatorAblation:
    """Compare only the locator-producing profile on identical fixed requests."""

    def __init__(self, native_runner, external_runner) -> None:
        self.native_runner = native_runner
        self.external_runner = external_runner

    def run(
        self,
        cases: tuple[InvestigatorEvalCase, ...],
    ) -> InvestigatorAblationReport:
        if not cases:
            raise ValueError("at least one eval case is required")
        return InvestigatorAblationReport(
            case_ids=tuple(case.case_id for case in cases),
            native=self._profile(self.native_runner, cases),
            external=self._profile(self.external_runner, cases),
        )

    @staticmethod
    def _profile(runner, cases) -> InvestigatorProfileMetrics:
        started = time.perf_counter()
        evidence_count = 0
        true_positives = 0
        expected_count = 0
        rejected = 0
        for case in cases:
            # Eval never enables fallback: a provider failure remains a result
            # for this profile instead of being filled with Native output.
            result = runner.run(case.request)
            actual = {
                (item.path, item.line_start, item.line_end)
                for item in result.evidence
            }
            expected = set(case.expected_locators)
            evidence_count += len(actual)
            expected_count += len(expected)
            true_positives += len(actual & expected)
            rejected += result.rejected_candidates
        precision = true_positives / evidence_count if evidence_count else 0.0
        recall = true_positives / expected_count if expected_count else 0.0
        return InvestigatorProfileMetrics(
            case_count=len(cases),
            evidence_count=evidence_count,
            true_positive_count=true_positives,
            expected_count=expected_count,
            evidence_precision=precision,
            evidence_recall=recall,
            duration_ms=int((time.perf_counter() - started) * 1_000),
            rejected_candidates=rejected,
        )
