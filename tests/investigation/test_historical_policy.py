from prd_agent.investigation.models import Requiredness
from prd_agent.investigation.policies import (
    CoverageTemplatePolicy,
    RequirednessPolicy,
)


def test_historical_context_coverage_template_contains_version_and_conflict_gates():
    coverage = CoverageTemplatePolicy().for_kind("HISTORICAL_PRD_CONTEXT")

    assert tuple(coverage) == (
        "historical_rule_found",
        "source_version_identified",
        "staleness_assessed",
        "code_conflict_checked",
        "decision_conflict_checked",
    )


def test_explicit_historical_prd_request_is_required():
    assert (
        RequirednessPolicy().decide(
            Requiredness.OPTIONAL, "请沿用历史 PRD 的订单状态方案"
        )
        == Requiredness.REQUIRED
    )
