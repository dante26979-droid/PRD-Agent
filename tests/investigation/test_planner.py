import unittest

from prd_agent.hashing import sha256_json
from prd_agent.investigation.planner import InformationNeedPlanner
from prd_agent.workflow.model import StructuredModelResult


class PlannerModel:
    def complete(self, operation, payload, *, repair=False):
        output = {
            "question": "当前订单金额字段如何存储？",
            "suggested_requiredness": "OPTIONAL",
            "need_kind": "FIELD_OR_FORMAT_CHANGE",
            "source_types": ["CODE"],
            "suggested_coverage": ["storage_schema"],
            "fallback": "ASK_USER_OR_MARK_UNKNOWN",
            "public_reason": "修改现有字段需要确认存储约束",
        }
        return StructuredModelResult(
            model_id="planner-stub",
            prompt_version="information-need.v1",
            structured_output=output,
            raw_output_hash=sha256_json(output),
        )


class InformationNeedPlannerTests(unittest.TestCase):
    def test_model_suggestion_is_validated_and_requiredness_is_upgraded(self) -> None:
        planner = InformationNeedPlanner(PlannerModel())
        need = planner.plan(
            {"requirement": "修改当前订单金额字段"},
            trigger_stage="UNIT_PREPARATION",
        )
        self.assertEqual(need.requiredness.value, "REQUIRED")
        self.assertEqual(need.required_coverage, ("storage_schema",))
        self.assertEqual(need.source_types, ("CODE",))


if __name__ == "__main__":
    unittest.main()
