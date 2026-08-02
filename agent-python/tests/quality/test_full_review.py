from __future__ import annotations

import hashlib

from agent.quality.full_review import FullDocumentQualityPolicy, FullReviewRequest
from agent.quality.models import QualityCode, QualityOutcome
from agent.unit import ConfirmedUnitContext, OutlineCandidate, OutlineNode, OutlineUnit


def _outline() -> OutlineCandidate:
    return OutlineCandidate(
        title="PRD",
        requirement_size="SMALL",
        nodes=(
            OutlineNode("goal", "需求目标", 10, "goal"),
            OutlineNode("solution", "方案说明", 20, "solution"),
        ),
        units=(
            OutlineUnit("goal", "需求目标", 10, ("goal",)),
            OutlineUnit("solution", "方案说明", 20, ("solution",), ("goal",)),
        ),
    )


def _context(key: str, markdown: str) -> ConfirmedUnitContext:
    return ConfirmedUnitContext(
        unit_key=key,
        unit_version=1,
        content_hash=hashlib.sha256(markdown.encode()).hexdigest(),
        markdown=markdown,
    )


def test_full_review_reports_missing_unit_without_modifying_content() -> None:
    report = FullDocumentQualityPolicy().evaluate(
        FullReviewRequest(_outline(), (_context("goal", "# 需求目标"),))
    )

    assert report.outcome is QualityOutcome.NEEDS_HUMAN
    assert report.issues[0].code is QualityCode.MISSING_REQUIRED_SECTION
    assert report.issues[0].affected_unit_keys == ("solution",)


def test_full_review_detects_inconsistent_terminology() -> None:
    report = FullDocumentQualityPolicy().evaluate(
        FullReviewRequest(
            _outline(),
            (
                _context("goal", "# 需求目标\n\n使用确认单元。"),
                _context("solution", "# 方案说明\n\n展示审批块。"),
            ),
            terminology={"Confirmation Unit": ("确认单元", "审批块")},
        )
    )

    assert {item.code for item in report.issues} == {
        QualityCode.INCONSISTENT_TERMINOLOGY
    }
