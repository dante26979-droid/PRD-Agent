from __future__ import annotations

import hashlib

from agent.unit.execution import ReviewableUnitExecutionModule
from agent.unit.models import (
    ConfirmedUnitContext,
    LockedOutline,
    OutlineCandidate,
    OutlineNode,
    OutlineUnit,
    RequirementBrief,
    RunOutputKind,
    RunPurpose,
    UnitExecutionRequest,
    UnitCandidate,
    UnitPatch,
    UnitResumeCursor,
    UnitScope,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _outline() -> OutlineCandidate:
    return OutlineCandidate(
        title="PRD",
        requirement_size="SMALL",
        nodes=(OutlineNode("goal", "目标", 10, "goal"),),
        units=(OutlineUnit("goal", "目标", 10, ("goal",)),),
    )


def test_full_review_uses_explicit_locked_outline_instead_of_encoded_ref() -> None:
    outline = _outline()
    markdown = "# 目标\n\n定义目标。"
    context = ConfirmedUnitContext("goal", 1, _hash(markdown), markdown=markdown)
    scope = UnitScope(
        purpose=RunPurpose.FULL_REVIEW,
        outline_id="outline-1",
        outline_version=1,
        outline_hash=outline.content_hash,
        confirmed_context=(context,),
    )
    request = UnitExecutionRequest(
        purpose=RunPurpose.FULL_REVIEW,
        scope=scope,
        requirement_brief=RequirementBrief("生成 PRD"),
        locked_outline=LockedOutline("outline-1", 1, outline),
        confirmed_context=(context,),
    )

    result = ReviewableUnitExecutionModule().run(request)

    assert result.output_kind is RunOutputKind.FULL_REVIEW_REPORT
    assert result.payload["outline_hash"] == outline.content_hash
    assert "replacement_markdown" not in result.payload


def test_revision_regrounds_and_rechecks_quality_before_returning_patch() -> None:
    outline = _outline()
    base = UnitCandidate(
        "goal", "目标", 10, ("goal",), "# 目标\n\n旧目标。", unknown_ids=("unknown-1",)
    )
    context = ConfirmedUnitContext(
        "goal", 1, base.content_hash, markdown=base.markdown
    )
    scope = UnitScope(
        purpose=RunPurpose.REVISE_UNIT,
        outline_id="outline-1",
        outline_version=1,
        outline_hash=outline.content_hash,
        current_unit_key="goal",
        current_unit_title="目标",
        current_unit_ordinal=10,
        section_node_keys=("goal",),
        confirmed_context=(context,),
        reopened_unit_keys=("goal",),
        base_unit_hash=base.content_hash,
        user_feedback="补充目标",
    )
    grounded_markdown: list[str] = []

    def ground(candidate: UnitCandidate) -> tuple[dict[str, object], ...]:
        grounded_markdown.append(candidate.markdown)
        return ({"status": "SUPPORTED", "claim_type": "CURRENT_STATE"},)

    patch = UnitPatch(
        "goal",
        base.content_hash,
        "# 目标\n\n新目标；待确认：负责人。",
        preserved_unknown_ids=("unknown-1",),
    )
    request = UnitExecutionRequest(
        purpose=RunPurpose.REVISE_UNIT,
        scope=scope,
        requirement_brief=RequirementBrief("生成 PRD", ("目标",)),
        locked_outline=LockedOutline("outline-1", 1, outline),
        confirmed_context=(context,),
        resume=UnitResumeCursor(2, 3, 4),
        base_candidate=base,
        unresolved_unknown_ids=("unknown-1",),
    )

    result = ReviewableUnitExecutionModule(
        unit_reviser=lambda _: patch,
        grounding_evaluator=ground,
    ).run(request)

    assert grounded_markdown == [patch.replacement_markdown]
    assert result.payload["requires_regrounding"] is False
    assert result.payload["claim_generation"] == 3
    assert result.payload["grounding_generation"] == 4
    assert result.payload["quality_generation"] == 5
