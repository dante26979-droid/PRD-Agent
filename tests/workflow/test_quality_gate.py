import unittest

from prd_agent.domain.commands import (
    ApproveRevisionPlan,
    ConfirmOutline,
    ConfirmUnit,
    FinalizePrd,
    StartTask,
)
from prd_agent.domain.errors import InvalidTransition
from prd_agent.domain.enums import UnitStatus
from prd_agent.quality.models import QualityIssueType
from prd_agent.quality.service import DocumentQualityService
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService

from .support import ScriptedModel, sufficient_brief
from .test_complete_workflow import two_unit_outline


class WorkflowQualityGateTests(unittest.TestCase):
    def test_document_quality_error_blocks_finalization(self) -> None:
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [two_unit_outline()],
                "generate_confirmation_unit": [
                    {"content": "### 规则\n\n- 支持开始时间筛选。"},
                    {"content": "### 验收\n\n- 体验良好。"},
                ],
            }
        )
        service = WorkflowService(
            InMemoryWorkflowRepository(),
            model,
            quality_service=DocumentQualityService(),
        )
        outline_wait = service.start_task(StartTask("增加时间筛选", "start"))
        first_wait = service.confirm_outline(
            ConfirmOutline(
                outline_wait.task.task_id,
                1,
                outline_wait.task.version,
                "confirm-outline",
            )
        )
        first = first_wait.current_outline.confirmation_units[0]
        second_wait = service.confirm_unit(
            ConfirmUnit(
                first_wait.task.task_id,
                first.unit_id,
                first_wait.task.version,
                "confirm-first",
            )
        )
        second = second_wait.current_outline.confirmation_units[1]
        review = service.confirm_unit(
            ConfirmUnit(
                second_wait.task.task_id,
                second.unit_id,
                second_wait.task.version,
                "confirm-second",
            )
        )

        self.assertFalse(review.quality_results[-1].confirmable)
        self.assertEqual(
            review.quality_results[-1].issues[0].issue_type,
            QualityIssueType.ACCEPTANCE_NOT_EXECUTABLE,
        )
        with self.assertRaises(InvalidTransition):
            service.finalize_prd(
                FinalizePrd(
                    review.task.task_id,
                    review.documents[-1].document_id,
                    review.documents[-1].content_hash,
                    review.task.version,
                    "finalize",
                )
            )

    def test_approved_quality_revision_creates_new_section_and_document_versions(self) -> None:
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [two_unit_outline()],
                "generate_confirmation_unit": [
                    {"content": "### 规则\n\n- 支持开始时间筛选。"},
                    {"content": "### 验收\n\n- 体验良好。"},
                    {
                        "content": (
                            "### 验收\n\n"
                            "- 当输入合法时间范围时，应只返回范围内订单。"
                        )
                    },
                ],
            }
        )
        service = WorkflowService(
            InMemoryWorkflowRepository(),
            model,
            quality_service=DocumentQualityService(),
        )
        outline_wait = service.start_task(StartTask("增加时间筛选", "start"))
        first_wait = service.confirm_outline(
            ConfirmOutline(
                outline_wait.task.task_id,
                1,
                outline_wait.task.version,
                "confirm-outline",
            )
        )
        first = first_wait.current_outline.confirmation_units[0]
        second_wait = service.confirm_unit(
            ConfirmUnit(
                first_wait.task.task_id,
                first.unit_id,
                first_wait.task.version,
                "confirm-first",
            )
        )
        second = second_wait.current_outline.confirmation_units[1]
        review_v1 = service.confirm_unit(
            ConfirmUnit(
                second_wait.task.task_id,
                second.unit_id,
                second_wait.task.version,
                "confirm-second",
            )
        )
        issue = review_v1.quality_results[-1].issues[0]

        revised_wait = service.approve_revision_plan(
            ApproveRevisionPlan(
                task_id=review_v1.task.task_id,
                document_id=review_v1.documents[-1].document_id,
                issue_ids=(issue.issue_id,),
                unit_ids=(second.unit_id,),
                expected_task_version=review_v1.task.version,
                idempotency_key="approve-revision",
            )
        )

        revised = revised_wait.current_outline.confirmation_units[1]
        self.assertEqual(revised.status, UnitStatus.PENDING_CONFIRMATION)
        review_v2 = service.confirm_unit(
            ConfirmUnit(
                revised_wait.task.task_id,
                revised.unit_id,
                revised_wait.task.version,
                "confirm-revision",
            )
        )

        self.assertEqual(len(review_v2.documents), 2)
        self.assertEqual(
            [item.version for item in review_v2.sections if item.unit_id == second.unit_id],
            [1, 2],
        )
        self.assertTrue(review_v2.quality_results[-1].confirmable)
        self.assertNotIn("体验良好", review_v2.documents[-1].markdown)
        self.assertIn("体验良好", review_v2.documents[0].markdown)


if __name__ == "__main__":
    unittest.main()
