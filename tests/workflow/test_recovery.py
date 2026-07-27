import unittest

from prd_agent.domain.commands import ConfirmOutline, StartTask
from prd_agent.domain.enums import UnitStatus
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService

from .support import ScriptedModel, first_outline, sufficient_brief


class RecoveryTests(unittest.TestCase):
    def test_new_service_resumes_from_persisted_business_state_without_regenerating_outline(self) -> None:
        repository = InMemoryWorkflowRepository()
        initial_model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [first_outline()],
            }
        )
        before_restart = WorkflowService(repository, initial_model).start_task(
            StartTask("订单列表增加创建时间筛选", "start")
        )
        resumed_model = ScriptedModel(
            {
                "generate_confirmation_unit": [
                    {"content": "### 验收标准\n\n- 日期范围内订单可被筛选。"}
                ]
            }
        )

        after_restart = WorkflowService(repository, resumed_model).confirm_outline(
            ConfirmOutline(
                before_restart.task.task_id,
                1,
                before_restart.task.version,
                "confirm-after-restart",
            )
        )

        self.assertEqual(resumed_model.calls["extract_requirement_brief"], 0)
        self.assertEqual(resumed_model.calls["generate_outline"], 0)
        self.assertEqual(
            after_restart.current_outline.confirmation_units[0].status,
            UnitStatus.PENDING_CONFIRMATION,
        )
        self.assertEqual(after_restart.run.thread_id, before_restart.run.thread_id)


if __name__ == "__main__":
    unittest.main()
