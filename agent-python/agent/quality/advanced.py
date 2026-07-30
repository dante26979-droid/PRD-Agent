from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from agent.draft import DraftBundle
from agent.grounding import GroundingOutcome


class QualityDisposition(StrEnum):
    REPAIRABLE = "REPAIRABLE"
    REQUIRES_GROUNDING = "REQUIRES_GROUNDING"
    NEEDS_HUMAN_DECISION = "NEEDS_HUMAN_DECISION"
    FATAL = "FATAL"


class QualityOutcome(StrEnum):
    PASSED = "QUALITY_PASSED"
    REPAIR_REQUIRED = "QUALITY_REPAIR_REQUIRED"
    NEEDS_HUMAN = "QUALITY_NEEDS_HUMAN"


@dataclass(frozen=True)
class QualityIssue:
    issue_id: str
    unit_key: str | None
    code: str
    disposition: QualityDisposition
    message: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "issue_id": self.issue_id,
            "unit_key": self.unit_key,
            "code": self.code,
            "disposition": self.disposition.value,
            "message": self.message,
        }


def check_advanced_quality(
    bundle: DraftBundle,
    *,
    grounding_outcome: GroundingOutcome,
    repair_count: int,
    max_repairs: int,
) -> tuple[tuple[QualityIssue, ...], QualityOutcome]:
    issues = []
    if grounding_outcome == GroundingOutcome.PARTIAL:
        issues.append(
            _issue(
                None,
                "UNSUPPORTED_CURRENT_STATE",
                QualityDisposition.NEEDS_HUMAN_DECISION,
                "存在未获得证据支持的当前状态事实。",
            )
        )
    seen = set()
    for unit in bundle.units:
        if not unit.markdown.strip():
            issues.append(
                _issue(
                    unit.unit_key,
                    "EMPTY_UNIT",
                    QualityDisposition.REPAIRABLE,
                    "Confirmation Unit 内容为空。",
                )
            )
        if unit.unit_key in seen:
            issues.append(
                _issue(
                    unit.unit_key,
                    "DUPLICATE_UNIT",
                    QualityDisposition.FATAL,
                    "Confirmation Unit key 重复。",
                )
            )
        seen.add(unit.unit_key)
    if any(
        item.disposition
        in {QualityDisposition.NEEDS_HUMAN_DECISION, QualityDisposition.FATAL}
        for item in issues
    ):
        return tuple(issues), QualityOutcome.NEEDS_HUMAN
    if issues:
        if repair_count < max_repairs:
            return tuple(issues), QualityOutcome.REPAIR_REQUIRED
        return tuple(issues), QualityOutcome.NEEDS_HUMAN
    return (), QualityOutcome.PASSED


def _issue(
    unit_key: str | None,
    code: str,
    disposition: QualityDisposition,
    message: str,
) -> QualityIssue:
    digest = hashlib.sha256(
        "\x00".join((unit_key or "", code, message)).encode("utf-8")
    ).hexdigest()[:20]
    return QualityIssue(
        issue_id="quality-" + digest,
        unit_key=unit_key,
        code=code,
        disposition=disposition,
        message=message,
    )
