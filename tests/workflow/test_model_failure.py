import unittest

from prd_agent.domain.commands import ConfirmOutline, StartTask
from prd_agent.domain.enums import RunStatus, TaskStatus, UnitStatus
from prd_agent.domain.errors import ModelOutputError
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService

from .support import ScriptedModel, first_outline, sufficient_brief


class ModelFailureTests(unittest.TestCase):
    def test_invalid_unit_is_repaired_once_then_persisted_as_failed(self) -> None:
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [first_outline()],
                "generate_confirmation_unit": [{"content": ""}, {"wrong": "shape"}],
            }
        )
        repository = InMemoryWorkflowRepository()
        service = WorkflowService(repository, model)
        waiting = service.start_task(StartTask("订单列表增加创建时间筛选", "start"))

        with self.assertRaises(ModelOutputError):
            service.confirm_outline(
                ConfirmOutline(
                    waiting.task.task_id,
                    1,
                    waiting.task.version,
                    "confirm-outline",
                )
            )

        failed = service.show(waiting.task.task_id)
        self.assertEqual(model.calls["generate_confirmation_unit"], 2)
        self.assertEqual(failed.task.status, TaskStatus.FAILED)
        self.assertEqual(failed.run.status, RunStatus.FAILED)
        self.assertEqual(
            failed.current_outline.confirmation_units[0].status,
            UnitStatus.FAILED,
        )
        self.assertIsNone(failed.current_outline.confirmation_units[0].content)


if __name__ == "__main__":
    unittest.main()
