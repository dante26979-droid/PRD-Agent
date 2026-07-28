import unittest

from pydantic import ValidationError

from prd_agent.investigation.models import (
    CoverageStatus,
    InformationNeed,
    Investigation,
    InvestigationBudget,
    Requiredness,
)
from prd_agent.investigation.policies import (
    CoverageTemplatePolicy,
    RequirednessPolicy,
    RoutePolicy,
)


SHA = "a" * 40


class InvestigationModelsAndPoliciesTests(unittest.TestCase):
    def test_none_need_cannot_require_coverage(self) -> None:
        with self.assertRaises(ValidationError):
            InformationNeed(
                information_need_id="need-1",
                question="需要查吗？",
                requiredness="NONE",
                source_types=(),
                required_coverage=("api_contract",),
                trigger_stage="UNIT_PREPARATION",
                fallback="CONTINUE",
                planner_version="v1",
                context_hash="sha256:x",
            )

    def test_required_need_requires_coverage(self) -> None:
        with self.assertRaises(ValidationError):
            InformationNeed(
                information_need_id="need-1",
                question="当前字段格式是什么？",
                requiredness="REQUIRED",
                source_types=("CODE",),
                required_coverage=(),
                trigger_stage="UNIT_PREPARATION",
                fallback="ASK_USER",
                planner_version="v1",
                context_hash="sha256:x",
            )

    def test_policy_upgrades_existing_field_change_to_required(self) -> None:
        decided = RequirednessPolicy().decide(
            Requiredness.OPTIONAL, "需要修改当前接口字段"
        )
        self.assertEqual(decided, Requiredness.REQUIRED)

    def test_coverage_template_is_missing_by_default(self) -> None:
        coverage = CoverageTemplatePolicy().for_kind("FIELD_OR_FORMAT_CHANGE")
        self.assertEqual(coverage["api_contract"].status, CoverageStatus.MISSING)
        self.assertEqual(coverage["storage_schema"].status, CoverageStatus.MISSING)

    def test_budget_has_hard_validation(self) -> None:
        with self.assertRaises(ValidationError):
            InvestigationBudget(max_iterations=0)

    def test_route_stops_before_exceeding_iteration_budget(self) -> None:
        investigation = Investigation(
            investigation_id="investigation-1",
            information_need_id="need-1",
            repository_id="demo",
            resolved_commit_sha=SHA,
            coverage=CoverageTemplatePolicy().build(("api_contract",)),
            budget=InvestigationBudget(max_iterations=1),
            iteration_count=1,
        )
        self.assertEqual(
            RoutePolicy().pre_action_stop(investigation).value,
            "MAX_ITERATIONS_REACHED",
        )


if __name__ == "__main__":
    unittest.main()
