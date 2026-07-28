import unittest

from prd_agent.domain.commands import ConfirmOutline, ConfirmUnit, StartTask
from prd_agent.domain.enums import RunStatus, TaskStatus, UnitStatus
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService

from .support import ScriptedModel, first_outline, sufficient_brief


class UnitConfirmationTests(unittest.TestCase):
    def test_confirmed_unit_is_snapshotted_and_rendered_to_markdown(self) -> None:
        unit_content = "### 业务规则\n\n- 支持闭区间创建时间筛选。"
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [first_outline()],
                "generate_confirmation_unit": [{"content": unit_content}],
            }
        )
        service = WorkflowService(InMemoryWorkflowRepository(), model)
        outline_wait = service.start_task(StartTask("订单列表增加创建时间筛选", "start"))
        unit_wait = service.confirm_outline(
            ConfirmOutline(
                outline_wait.task.task_id,
                1,
                outline_wait.task.version,
                "confirm-outline",
            )
        )
        unit = unit_wait.current_outline.confirmation_units[0]

        completed = service.confirm_unit(
            ConfirmUnit(
                task_id=unit_wait.task.task_id,
                unit_id=unit.unit_id,
                expected_task_version=unit_wait.task.version,
                idempotency_key="confirm-unit",
            )
        )

        self.assertEqual(completed.task.status, TaskStatus.FINAL_REVIEW)
        self.assertEqual(completed.run.status, RunStatus.SUCCEEDED)
        self.assertEqual(
            completed.current_outline.confirmation_units[0].status,
            UnitStatus.CONFIRMED,
        )
        self.assertEqual(len(completed.sections), 1)
        self.assertEqual(completed.sections[0].content, unit_content)
        self.assertIn("# 订单创建时间筛选", completed.markdown)
        self.assertIn("## 需求摘要", completed.markdown)
        self.assertIn("## 第一确认单元：筛选规则与验收", completed.markdown)
        self.assertIn(unit_content, completed.markdown)


if __name__ == "__main__":
    unittest.main()
