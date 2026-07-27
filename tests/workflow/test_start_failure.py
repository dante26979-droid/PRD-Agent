import unittest

from prd_agent.domain.commands import StartTask
from prd_agent.domain.enums import RunStatus, TaskStatus
from prd_agent.domain.errors import ModelOutputError
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService

from .support import ScriptedModel


class StartFailureTests(unittest.TestCase):
    def test_invalid_brief_is_repaired_once_and_preserves_input_in_failed_task(self) -> None:
        repository = InMemoryWorkflowRepository()
        model = ScriptedModel(
            {"extract_requirement_brief": [{"problem": ""}, {"wrong": "shape"}]}
        )
        service = WorkflowService(repository, model)

        with self.assertRaises(ModelOutputError):
            service.start_task(StartTask("订单列表增加创建时间筛选", "bad-brief"))

        self.assertEqual(len(repository.tasks), 1)
        task_id = next(iter(repository.tasks))
        failed = service.show(task_id)
        self.assertEqual(model.calls["extract_requirement_brief"], 2)
        self.assertEqual(failed.task.status, TaskStatus.FAILED)
        self.assertEqual(failed.run.status, RunStatus.FAILED)
        self.assertEqual(failed.messages[0].content, "订单列表增加创建时间筛选")
        self.assertFalse(failed.current_brief.confirmed)


if __name__ == "__main__":
    unittest.main()
