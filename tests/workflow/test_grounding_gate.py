import unittest

from prd_agent.domain.commands import ConfirmOutline, StartTask
from prd_agent.domain.enums import TaskStatus, UnitStatus
from prd_agent.grounding.service import GroundingService
from prd_agent.evidence.models import (
    DeterministicFact,
    ExtractionMethod,
    FactType,
    SourceEvidence,
    VerificationStatus,
)
from prd_agent.domain.commands import ConfirmUnit
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService

from .support import ScriptedModel, first_outline, sufficient_brief


class WorkflowGroundingGateTests(unittest.TestCase):
    def test_unsupported_critical_claim_never_becomes_confirmable_content(self) -> None:
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [first_outline()],
                "generate_confirmation_unit": [
                    {
                        "content": "### 当前能力\n\n当前系统支持批量导入。",
                        "claims": [
                            {
                                "claim_id": "claim-batch",
                                "text": "当前系统支持批量导入。",
                                "kind": "CURRENT_STATE",
                                "criticality": "CRITICAL",
                                "fact_ids": ["fact-missing"],
                            }
                        ],
                    }
                ],
            }
        )

        service = WorkflowService(
            InMemoryWorkflowRepository(),
            model,
            unit_context_provider=lambda **_: {
                "repository_id": "demo",
                "resolved_commit_sha": "a" * 40,
                "facts": [],
                "evidence": [],
                "conflicts": [],
            },
            grounding_service=GroundingService(),
        )
        outline_wait = service.start_task(StartTask("增加批量导入", "start"))
        blocked = service.confirm_outline(
            ConfirmOutline(
                outline_wait.task.task_id,
                outline_version=1,
                expected_task_version=outline_wait.task.version,
                idempotency_key="confirm-outline",
            )
        )

        self.assertEqual(blocked.task.status, TaskStatus.GENERATING)
        self.assertEqual(
            blocked.current_outline.confirmation_units[0].status,
            UnitStatus.HUMAN_INPUT_REQUIRED,
        )
        self.assertIsNone(blocked.current_outline.confirmation_units[0].content)
        self.assertIsNone(blocked.markdown)

    def test_unlisted_current_state_assertion_is_blocked_by_claim_inventory_guard(
        self,
    ) -> None:
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [first_outline()],
                "generate_confirmation_unit": [
                    {"content": "### 当前能力\n\n当前系统支持批量导入。"}
                ],
            }
        )
        service = WorkflowService(
            InMemoryWorkflowRepository(),
            model,
            unit_context_provider=lambda **_: {
                "repository_id": "demo",
                "resolved_commit_sha": "a" * 40,
                "facts": [],
                "evidence": [],
                "conflicts": [],
            },
            grounding_service=GroundingService(),
        )
        outline_wait = service.start_task(StartTask("增加批量导入", "start-inventory"))
        blocked = service.confirm_outline(
            ConfirmOutline(
                outline_wait.task.task_id,
                1,
                outline_wait.task.version,
                "confirm-inventory",
            )
        )

        self.assertEqual(
            blocked.current_outline.confirmation_units[0].status,
            UnitStatus.HUMAN_INPUT_REQUIRED,
        )
        self.assertIsNone(blocked.current_outline.confirmation_units[0].content)

    def test_supported_claim_flows_into_the_final_evidence_appendix(self) -> None:
        commit = "a" * 40
        evidence = SourceEvidence(
            evidence_id="evidence-filter",
            tool_call_id="call-filter",
            repository_id="demo",
            resolved_commit_sha=commit,
            path="src/orders/filter.py",
            line_start=10,
            line_end=14,
            excerpt="订单列表支持开始时间筛选。",
            content_hash="sha256:filter",
            extraction_method=ExtractionMethod.SOURCE_READ,
        )
        fact = DeterministicFact(
            fact_id="fact-filter",
            tool_call_id="call-filter",
            task_id="task-placeholder",
            subject="订单列表",
            predicate="支持",
            value_json="开始时间筛选",
            fact_type=FactType.CODE_VERIFIED,
            confidence="HIGH",
            verification_status=VerificationStatus.SUPPORTED,
            extractor_id="test",
            extractor_version="1",
            evidence_ids=("evidence-filter",),
        )
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [first_outline()],
                "generate_confirmation_unit": [
                    {
                        "content": "### 当前能力\n\n当前订单列表支持开始时间筛选。",
                        "claims": [
                            {
                                "claim_id": "claim-filter",
                                "text": "当前订单列表支持开始时间筛选。",
                                "kind": "CURRENT_STATE",
                                "criticality": "CRITICAL",
                                "fact_ids": ["fact-filter"],
                            }
                        ],
                    }
                ],
            }
        )
        repository = InMemoryWorkflowRepository()

        def context_provider(**kwargs):
            task_fact = fact.model_copy(update={"task_id": kwargs["task"].task_id})
            return {
                "repository_id": "demo",
                "resolved_commit_sha": commit,
                "facts": [task_fact],
                "evidence": [evidence],
                "conflicts": [],
            }

        service = WorkflowService(
            repository,
            model,
            unit_context_provider=context_provider,
            grounding_service=GroundingService(),
        )
        outline_wait = service.start_task(StartTask("增加时间筛选", "start-supported"))
        unit_wait = service.confirm_outline(
            ConfirmOutline(
                outline_wait.task.task_id,
                1,
                outline_wait.task.version,
                "confirm-outline-supported",
            )
        )
        unit = unit_wait.current_outline.confirmation_units[0]
        review = service.confirm_unit(
            ConfirmUnit(
                unit_wait.task.task_id,
                unit.unit_id,
                unit_wait.task.version,
                "confirm-unit-supported",
            )
        )

        self.assertIn("## 参考依据", review.markdown)
        self.assertIn("src/orders/filter.py:10-14", review.markdown)
        self.assertEqual(len(review.grounding_results), 1)


if __name__ == "__main__":
    unittest.main()
