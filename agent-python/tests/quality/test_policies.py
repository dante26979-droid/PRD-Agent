from __future__ import annotations

from agent.quality.models import QualityCode, QualityOutcome
from agent.quality.policies import DocumentQualityPolicy, UnitQualityRequest
from agent.unit import UnitCandidate


def _candidate(markdown: str) -> UnitCandidate:
    return UnitCandidate(
        unit_key="acceptance",
        title="验收标准",
        ordinal=20,
        node_keys=("acceptance",),
        markdown=markdown,
    )


def test_acceptance_criterion_requires_precondition_trigger_and_expected_result() -> None:
    report = DocumentQualityPolicy().evaluate_unit(
        UnitQualityRequest(
            candidate=_candidate("# 验收标准\n\n- 功能正常。"),
            required_content=("precondition", "trigger", "expected_result"),
        )
    )

    assert report.outcome is QualityOutcome.REPAIR_REQUIRED
    assert {item.code for item in report.issues} == {
        QualityCode.AMBIGUOUS_ACCEPTANCE_CRITERION
    }


def test_grounding_conflict_unknown_and_sensitive_content_have_stable_codes() -> None:
    report = DocumentQualityPolicy().evaluate_unit(
        UnitQualityRequest(
            candidate=_candidate("# 验收标准\n\nAuthorization: secret"),
            grounding_findings=(
                {"status": "UNSUPPORTED", "claim_type": "CURRENT_STATE"},
            ),
            unresolved_conflict_ids=("conflict-1",),
            unresolved_unknown_ids=("unknown-1",),
        )
    )

    assert report.outcome is QualityOutcome.NEEDS_HUMAN
    assert {item.code for item in report.issues} == {
        QualityCode.SENSITIVE_CONTENT,
        QualityCode.UNSUPPORTED_CURRENT_STATE,
        QualityCode.UNRESOLVED_SOURCE_CONFLICT,
        QualityCode.UNMARKED_UNKNOWN,
    }


def test_complete_acceptance_criterion_passes() -> None:
    report = DocumentQualityPolicy().evaluate_unit(
        UnitQualityRequest(
            candidate=_candidate(
                "# 验收标准\n\n- 前置条件：用户已登录；用户操作：点击提交；预期结果：系统保存成功。"
            ),
            required_content=("precondition", "trigger", "expected_result"),
        )
    )

    assert report.outcome is QualityOutcome.PASSED
    assert report.issues == ()
