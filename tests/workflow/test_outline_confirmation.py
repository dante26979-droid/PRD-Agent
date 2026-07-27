import unittest

from prd_agent.domain.commands import ConfirmOutline, StartTask
from prd_agent.domain.enums import OutlineStatus, RunStatus, TaskStatus, UnitStatus
from prd_agent.domain.errors import VersionConflict
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService

from .support import ScriptedModel, first_outline, sufficient_brief


class OutlineConfirmationTests(unittest.TestCase):
    def test_confirmation_locks_outline_and_generates_only_first_unit(self) -> None:
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [first_outline()],
                "generate_confirmation_unit": [
                    {"content": "### 业务规则\n\n- 支持开始时间和结束时间筛选。"}
                ],
            }
        )
        service = WorkflowService(InMemoryWorkflowRepository(), model)
        waiting = service.start_task(StartTask("订单列表增加创建时间筛选", "start"))

        with self.assertRaises(VersionConflict):
            service.confirm_outline(
                ConfirmOutline(
                    waiting.task.task_id,
                    outline_version=1,
                    expected_task_version=waiting.task.version - 1,
                    idempotency_key="stale-confirm",
                )
            )

        generated = service.confirm_outline(
            ConfirmOutline(
                waiting.task.task_id,
                outline_version=1,
                expected_task_version=waiting.task.version,
                idempotency_key="confirm-outline",
            )
        )

        self.assertEqual(generated.task.status, TaskStatus.GENERATING)
        self.assertEqual(generated.run.status, RunStatus.WAITING_USER)
        self.assertEqual(generated.current_outline.status, OutlineStatus.CONFIRMED)
        self.assertEqual(len(generated.current_outline.confirmation_units), 1)
        unit = generated.current_outline.confirmation_units[0]
        self.assertEqual(unit.sequence, 1)
        self.assertEqual(unit.status, UnitStatus.PENDING_CONFIRMATION)
        self.assertIn("开始时间", unit.content)


if __name__ == "__main__":
    unittest.main()
