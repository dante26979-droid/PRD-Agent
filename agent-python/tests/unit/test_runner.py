from __future__ import annotations

import hashlib
import json

from agent.quality.models import QualityOutcome
from agent.unit import (
    ConfirmedUnitContext,
    OutlineCandidate,
    OutlineNode,
    OutlineUnit,
    ReviewableUnitModule,
    RunOutputKind,
    RunPurpose,
    UnitCandidate,
    UnitPatch,
    UnitRunRequest,
    UnitScope,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _outline() -> OutlineCandidate:
    return OutlineCandidate(
        title="PRD",
        requirement_size="SMALL",
        nodes=(OutlineNode("acceptance", "验收标准", 10, "acceptance"),),
        units=(OutlineUnit("acceptance", "验收标准", 10, ("acceptance",)),),
    )


def test_generate_unit_returns_only_current_candidate_and_quality_report() -> None:
    scope = UnitScope(
        purpose=RunPurpose.GENERATE_UNIT,
        outline_id="outline-1",
        outline_version=1,
        outline_hash=_outline().content_hash,
        current_unit_key="acceptance",
        current_unit_title="验收标准",
        current_unit_ordinal=10,
        section_node_keys=("acceptance",),
        requirement_brief_ref="required:precondition,trigger,expected_result",
    )
    candidate = UnitCandidate(
        "acceptance",
        "验收标准",
        10,
        ("acceptance",),
        "# 验收标准\n\n- 前置条件：登录；触发条件：提交；预期结果：成功。",
    )

    result = ReviewableUnitModule(unit_generator=lambda _: candidate).run(
        UnitRunRequest(RunPurpose.GENERATE_UNIT, scope, "需求")
    )

    assert result.output_kind is RunOutputKind.UNIT_CANDIDATE
    assert result.payload["unit_key"] == "acceptance"
    assert result.payload["quality_report"]["outcome"] == QualityOutcome.PASSED.value


def test_revision_result_requires_regrounding() -> None:
    base = UnitCandidate(
        "acceptance",
        "验收标准",
        10,
        ("acceptance",),
        "# 验收标准\n\n旧内容。",
        unknown_ids=("unknown-1",),
    )
    scope = UnitScope(
        purpose=RunPurpose.REVISE_UNIT,
        outline_id="outline-1",
        outline_version=1,
        outline_hash=_outline().content_hash,
        current_unit_key="acceptance",
        current_unit_title="验收标准",
        current_unit_ordinal=10,
        section_node_keys=("acceptance",),
        reopened_unit_keys=("acceptance",),
        base_unit_hash=base.content_hash,
        user_feedback="补充条件",
    )
    patch = UnitPatch(
        "acceptance",
        base.content_hash,
        "# 验收标准\n\n待确认：接口状态。",
        preserved_unknown_ids=("unknown-1",),
    )

    result = ReviewableUnitModule(unit_reviser=lambda _: patch).run(
        UnitRunRequest(
            RunPurpose.REVISE_UNIT,
            scope,
            "需求",
            base_candidate=base,
            unresolved_unknown_ids=("unknown-1",),
        )
    )

    assert result.output_kind is RunOutputKind.UNIT_PATCH
    assert result.payload["requires_regrounding"] is True


def test_full_review_returns_report_and_never_unit_patch() -> None:
    outline = _outline()
    markdown = "# 验收标准\n\n内容"
    context = ConfirmedUnitContext(
        "acceptance", 1, _hash(markdown), markdown=markdown
    )
    scope = UnitScope(
        purpose=RunPurpose.FULL_REVIEW,
        outline_id="outline-1",
        outline_version=1,
        outline_hash=outline.content_hash,
        confirmed_context=(context,),
        requirement_brief_ref="outline-json:"
        + json.dumps(outline.as_dict(), ensure_ascii=False),
    )

    result = ReviewableUnitModule().run(
        UnitRunRequest(RunPurpose.FULL_REVIEW, scope, "需求")
    )

    assert result.output_kind is RunOutputKind.FULL_REVIEW_REPORT
    assert "replacement_markdown" not in result.payload
    assert result.payload["unit_hashes"] == {"acceptance": _hash(markdown)}
