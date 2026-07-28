import unittest

from prd_agent.domain.commands import (
    ConfirmOutline,
    ConfirmUnit,
    FinalizePrd,
    ReopenPrd,
    StartTask,
)
from prd_agent.domain.enums import TaskStatus, UnitStatus
from prd_agent.domain.errors import VersionConflict
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.quality.service import DocumentQualityService
from prd_agent.workflow.service import WorkflowService

from .support import ScriptedModel, sufficient_brief


def two_unit_outline() -> dict:
    return {
        "title": "订单创建时间筛选",
        "nodes": [
            {
                "key": "rules",
                "title": "筛选规则",
                "purpose": "定义筛选规则",
                "complexity": "MEDIUM",
                "required_information": [],
            },
            {
                "key": "acceptance",
                "title": "验收标准",
                "purpose": "定义可执行验收",
                "complexity": "MEDIUM",
                "required_information": [],
            },
        ],
        "units": [
            {
                "key": "unit-rules",
                "title": "筛选规则",
                "node_keys": ["rules"],
                "depends_on_unit_keys": [],
            },
            {
                "key": "unit-acceptance",
                "title": "验收标准",
                "node_keys": ["acceptance"],
                "depends_on_unit_keys": ["unit-rules"],
            },
        ],
    }


class CompleteWorkflowTests(unittest.TestCase):
    def test_two_units_are_confirmed_before_explicit_finalization(self) -> None:
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [two_unit_outline()],
                "generate_confirmation_unit": [
                    {"content": "### 规则\n\n- 支持开始时间和结束时间。"},
                    {
                        "content": (
                            "### 验收\n\n"
                            "- 当输入合法时间范围时，应只返回范围内订单。"
                        )
                    },
                ],
            }
        )
        service = WorkflowService(InMemoryWorkflowRepository(), model)
        outline_wait = service.start_task(StartTask("订单列表增加创建时间筛选", "start"))
        first_wait = service.confirm_outline(
            ConfirmOutline(
                outline_wait.task.task_id,
                outline_version=1,
                expected_task_version=outline_wait.task.version,
                idempotency_key="confirm-outline",
            )
        )

        first = first_wait.current_outline.confirmation_units[0]
        second_wait = service.confirm_unit(
            ConfirmUnit(
                task_id=first_wait.task.task_id,
                unit_id=first.unit_id,
                expected_task_version=first_wait.task.version,
                idempotency_key="confirm-first",
            )
        )

        self.assertEqual(second_wait.task.status, TaskStatus.GENERATING)
        self.assertEqual(
            second_wait.current_outline.confirmation_units[0].status,
            UnitStatus.CONFIRMED,
        )
        second = second_wait.current_outline.confirmation_units[1]
        self.assertEqual(second.status, UnitStatus.PENDING_CONFIRMATION)

        review = service.confirm_unit(
            ConfirmUnit(
                task_id=second_wait.task.task_id,
                unit_id=second.unit_id,
                expected_task_version=second_wait.task.version,
                idempotency_key="confirm-second",
            )
        )

        self.assertEqual(review.task.status, TaskStatus.FINAL_REVIEW)
        self.assertEqual(len(review.sections), 2)
        self.assertIn("## 筛选规则", review.markdown)
        self.assertIn("## 验收标准", review.markdown)

        completed = service.finalize_prd(
            FinalizePrd(
                task_id=review.task.task_id,
                document_id=review.documents[-1].document_id,
                content_hash=review.documents[-1].content_hash,
                expected_task_version=review.task.version,
                idempotency_key="finalize",
            )
        )

        self.assertEqual(completed.task.status, TaskStatus.COMPLETED)
        self.assertEqual(completed.documents[-1].status.value, "CONFIRMED")

        replayed = service.finalize_prd(
            FinalizePrd(
                task_id=review.task.task_id,
                document_id=review.documents[-1].document_id,
                content_hash=review.documents[-1].content_hash,
                expected_task_version=review.task.version,
                idempotency_key="finalize",
            )
        )
        self.assertEqual(replayed.task.version, completed.task.version)
        self.assertEqual(len(replayed.documents), 1)

    def test_stale_document_hash_cannot_be_finalized(self) -> None:
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [
                    {
                        "title": "时间筛选",
                        "nodes": [
                            {
                                "title": "规则",
                                "purpose": "定义规则",
                                "complexity": "LOW",
                                "required_information": [],
                            }
                        ],
                    }
                ],
                "generate_confirmation_unit": [{"content": "### 规则\n\n- 支持筛选。"}],
            }
        )
        service = WorkflowService(InMemoryWorkflowRepository(), model)
        outline_wait = service.start_task(StartTask("增加筛选", "hash-start"))
        unit_wait = service.confirm_outline(
            ConfirmOutline(
                outline_wait.task.task_id,
                1,
                outline_wait.task.version,
                "hash-outline",
            )
        )
        unit = unit_wait.current_outline.confirmation_units[0]
        review = service.confirm_unit(
            ConfirmUnit(
                unit_wait.task.task_id,
                unit.unit_id,
                unit_wait.task.version,
                "hash-unit",
            )
        )

        with self.assertRaises(VersionConflict):
            service.finalize_prd(
                FinalizePrd(
                    review.task.task_id,
                    review.documents[-1].document_id,
                    "sha256:stale",
                    review.task.version,
                    "hash-finalize",
                )
            )

    def test_completed_document_can_be_reopened_without_overwriting_history(self) -> None:
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [two_unit_outline()],
                "generate_confirmation_unit": [
                    {"content": "### 规则\n\n- 规则 v1。"},
                    {
                        "content": (
                            "### 验收\n\n"
                            "- 当输入合法时间时，应返回范围内订单。"
                        )
                    },
                    {"content": "### 规则\n\n- 规则 v2。"},
                    {
                        "content": (
                            "### 验收\n\n"
                            "- 当输入合法时间时，应按规则 v2 返回订单。"
                        )
                    },
                ],
            }
        )
        service = WorkflowService(InMemoryWorkflowRepository(), model)
        outline_wait = service.start_task(StartTask("增加时间筛选", "reopen-start"))
        first_wait = service.confirm_outline(
            ConfirmOutline(
                outline_wait.task.task_id,
                1,
                outline_wait.task.version,
                "reopen-outline",
            )
        )
        first = first_wait.current_outline.confirmation_units[0]
        second_wait = service.confirm_unit(
            ConfirmUnit(
                first_wait.task.task_id,
                first.unit_id,
                first_wait.task.version,
                "reopen-first",
            )
        )
        second = second_wait.current_outline.confirmation_units[1]
        review_v1 = service.confirm_unit(
            ConfirmUnit(
                second_wait.task.task_id,
                second.unit_id,
                second_wait.task.version,
                "reopen-second",
            )
        )
        completed_v1 = service.finalize_prd(
            FinalizePrd(
                review_v1.task.task_id,
                review_v1.documents[-1].document_id,
                review_v1.documents[-1].content_hash,
                review_v1.task.version,
                "reopen-finalize-v1",
            )
        )

        revised_first_wait = service.reopen_prd(
            ReopenPrd(
                task_id=completed_v1.task.task_id,
                unit_ids=(first.unit_id,),
                reason="业务规则发生变化",
                expected_task_version=completed_v1.task.version,
                idempotency_key="reopen-prd",
            )
        )
        revised_first = revised_first_wait.current_outline.confirmation_units[0]
        revised_second_wait = service.confirm_unit(
            ConfirmUnit(
                revised_first_wait.task.task_id,
                revised_first.unit_id,
                revised_first_wait.task.version,
                "reopen-confirm-first-v2",
            )
        )
        revised_second = revised_second_wait.current_outline.confirmation_units[1]
        self.assertEqual(revised_second.status, UnitStatus.PENDING_CONFIRMATION)
        review_v2 = service.confirm_unit(
            ConfirmUnit(
                revised_second_wait.task.task_id,
                revised_second.unit_id,
                revised_second_wait.task.version,
                "reopen-confirm-second-v2",
            )
        )

        self.assertEqual(len(review_v2.documents), 2)
        self.assertIn("规则 v1", review_v2.documents[0].markdown)
        self.assertNotIn("规则 v2", review_v2.documents[0].markdown)
        self.assertIn("规则 v2", review_v2.documents[1].markdown)

    def test_one_confirmation_unit_can_confirm_multiple_outline_sections(self) -> None:
        outline = two_unit_outline()
        outline["units"] = [
            {
                "key": "unit-combined",
                "title": "规则与验收",
                "node_keys": ["rules", "acceptance"],
                "depends_on_unit_keys": [],
            }
        ]
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [outline],
                "generate_confirmation_unit": [
                    {
                        "sections": [
                            {
                                "node_key": "rules",
                                "title": "模型擅自改名",
                                "content": "- 支持开始时间和结束时间筛选。",
                            },
                            {
                                "node_key": "acceptance",
                                "title": "验收标准",
                                "content": (
                                    "- 当输入合法时间范围时，应只返回范围内订单。"
                                ),
                            },
                        ]
                    }
                ],
            }
        )
        service = WorkflowService(
            InMemoryWorkflowRepository(),
            model,
            quality_service=DocumentQualityService(),
        )
        outline_wait = service.start_task(StartTask("增加时间筛选", "combined-start"))
        unit_wait = service.confirm_outline(
            ConfirmOutline(
                outline_wait.task.task_id,
                1,
                outline_wait.task.version,
                "combined-outline",
            )
        )
        unit = unit_wait.current_outline.confirmation_units[0]
        review = service.confirm_unit(
            ConfirmUnit(
                unit_wait.task.task_id,
                unit.unit_id,
                unit_wait.task.version,
                "combined-unit",
            )
        )

        self.assertEqual(len(review.sections), 2)
        self.assertEqual(
            {item.node_id for item in review.sections},
            set(unit.node_ids),
        )
        self.assertTrue(review.quality_results[-1].confirmable)
        self.assertIn("## 筛选规则", review.markdown)
        self.assertIn("## 验收标准", review.markdown)
        self.assertNotIn("模型擅自改名", review.markdown)


if __name__ == "__main__":
    unittest.main()
