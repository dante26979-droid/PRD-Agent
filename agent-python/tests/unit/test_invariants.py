from __future__ import annotations

import hashlib

import pytest

from agent.unit import (
    RunPurpose,
    UnitCandidate,
    UnitInvariantError,
    UnitInvariantPolicy,
    UnitPatch,
    UnitRunRequest,
    UnitScope,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _scope(*, revise: bool = False) -> UnitScope:
    return UnitScope(
        purpose=RunPurpose.REVISE_UNIT if revise else RunPurpose.GENERATE_UNIT,
        outline_id="outline-1",
        outline_version=1,
        outline_hash=_hash("outline"),
        current_unit_key="acceptance",
        current_unit_title="验收标准",
        current_unit_ordinal=20,
        section_node_keys=("acceptance",),
        reopened_unit_keys=("acceptance",) if revise else (),
        base_unit_hash=_candidate().content_hash if revise else "",
        user_feedback="补充可验证结果" if revise else "",
    )


def _candidate() -> UnitCandidate:
    return UnitCandidate(
        unit_key="acceptance",
        title="验收标准",
        ordinal=20,
        node_keys=("acceptance",),
        markdown="# 验收标准\n\n- 前置条件：已登录；触发条件：提交；预期结果：保存成功。",
        unknown_ids=("unknown-1",),
        used_fact_ids=("fact-1",),
    )


def test_candidate_must_match_locked_current_unit() -> None:
    candidate = _candidate()
    escaped = UnitCandidate(
        unit_key="other",
        title=candidate.title,
        ordinal=candidate.ordinal,
        node_keys=candidate.node_keys,
        markdown=candidate.markdown,
    )
    request = UnitRunRequest(RunPurpose.GENERATE_UNIT, _scope(), "需求")

    with pytest.raises(UnitInvariantError, match="current unit"):
        UnitInvariantPolicy().validate_candidate(request, escaped)


def test_candidate_heading_must_use_locked_title() -> None:
    candidate = UnitCandidate(
        unit_key="acceptance",
        title="验收标准",
        ordinal=20,
        node_keys=("acceptance",),
        markdown="# 新章节\n\n内容",
    )
    request = UnitRunRequest(RunPurpose.GENERATE_UNIT, _scope(), "需求")

    with pytest.raises(UnitInvariantError, match="locked title"):
        UnitInvariantPolicy().validate_candidate(request, candidate)


def test_patch_rejects_stale_hash_removed_unknown_and_unavailable_fact() -> None:
    base = _candidate()
    request = UnitRunRequest(
        RunPurpose.REVISE_UNIT,
        _scope(revise=True),
        "需求",
        base_candidate=base,
        unresolved_unknown_ids=("unknown-1",),
        available_fact_ids=("fact-1",),
    )
    policy = UnitInvariantPolicy()

    with pytest.raises(UnitInvariantError, match="stale"):
        policy.validate_patch(
            request,
            UnitPatch("acceptance", _hash("stale"), "# 验收标准\n\n修订"),
        )
    with pytest.raises(UnitInvariantError, match="unknown"):
        policy.validate_patch(
            request,
            UnitPatch("acceptance", base.content_hash, "# 验收标准\n\n修订"),
        )
    with pytest.raises(UnitInvariantError, match="unavailable facts"):
        policy.validate_patch(
            request,
            UnitPatch(
                "acceptance",
                base.content_hash,
                "# 验收标准\n\n修订",
                preserved_unknown_ids=("unknown-1",),
                used_fact_ids=("fact-2",),
            ),
        )
