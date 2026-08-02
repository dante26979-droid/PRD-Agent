from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from agent.quality.models import (
    DocumentQualityIssue,
    DocumentQualityReport,
    QualityCode,
    QualityDisposition,
    QualityOutcome,
    QualityScope,
    QualitySeverity,
)
from agent.unit.models import ConfirmedUnitContext, OutlineCandidate


@dataclass(frozen=True)
class FullReviewRequest:
    outline: OutlineCandidate
    units: tuple[ConfirmedUnitContext, ...]
    terminology: Mapping[str, tuple[str, ...]] | None = None
    unresolved_conflicts: Mapping[str, tuple[str, ...]] | None = None


class FullDocumentQualityPolicy:
    def evaluate(self, request: FullReviewRequest) -> DocumentQualityReport:
        contexts = {item.unit_key: item for item in request.units}
        issues: list[DocumentQualityIssue] = []
        for unit in request.outline.units:
            context = contexts.get(unit.unit_key)
            if context is None or not context.markdown.strip():
                issues.append(
                    _issue(
                        QualityCode.MISSING_REQUIRED_SECTION,
                        QualityDisposition.NEEDS_HUMAN,
                        "锁定大纲中的 Confirmation Unit 尚未完成。",
                        (unit.unit_key,),
                    )
                )
                continue
            actual = hashlib.sha256(context.markdown.encode("utf-8")).hexdigest()
            if actual != context.content_hash.removeprefix("sha256:"):
                issues.append(
                    _issue(
                        QualityCode.IMMUTABLE_UNIT_CHANGED,
                        QualityDisposition.FATAL,
                        "已确认 Unit 正文与冻结 hash 不一致。",
                        (unit.unit_key,),
                    )
                )
        document = "\n".join(item.markdown for item in request.units)
        for canonical, aliases in (request.terminology or {}).items():
            used = tuple(alias for alias in aliases if alias and alias in document)
            if len(used) > 1:
                affected = tuple(
                    sorted(
                        item.unit_key
                        for item in request.units
                        if any(alias in item.markdown for alias in used)
                    )
                )
                issues.append(
                    _issue(
                        QualityCode.INCONSISTENT_TERMINOLOGY,
                        QualityDisposition.NEEDS_HUMAN,
                        f"术语 {canonical} 同时使用了多个名称：{', '.join(used)}。",
                        affected,
                    )
                )
        for code, affected in (request.unresolved_conflicts or {}).items():
            quality_code = QualityCode(code)
            issues.append(
                _issue(
                    quality_code,
                    QualityDisposition.NEEDS_HUMAN,
                    "全文检查发现尚未解决的一致性问题。",
                    tuple(affected),
                )
            )
        return DocumentQualityReport(
            scope=QualityScope.FULL_DOCUMENT,
            outcome=QualityOutcome.PASSED if not issues else QualityOutcome.NEEDS_HUMAN,
            issues=tuple(_deduplicate(issues)),
            unit_hashes={item.unit_key: item.content_hash.removeprefix("sha256:") for item in request.units},
        )


def _issue(
    code: QualityCode,
    disposition: QualityDisposition,
    message: str,
    affected: tuple[str, ...],
) -> DocumentQualityIssue:
    return DocumentQualityIssue(
        code=code,
        severity=QualitySeverity.BLOCKING,
        disposition=disposition,
        message=message,
        affected_unit_keys=affected,
    )


def _deduplicate(issues: list[DocumentQualityIssue]) -> list[DocumentQualityIssue]:
    return list({item.issue_id: item for item in issues}.values())
