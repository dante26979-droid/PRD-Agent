import unittest

from prd_agent.domain.commands import StartTask
from prd_agent.domain.enums import RunStatus, TaskStatus
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService

from .support import ScriptedModel, first_outline, sufficient_brief


class StartTaskTests(unittest.TestCase):
    def test_start_creates_outline_review_and_replays_same_command(self) -> None:
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [first_outline()],
            }
        )
        service = WorkflowService(InMemoryWorkflowRepository(), model)
        command = StartTask(
            message="订单列表增加创建时间筛选",
            idempotency_key="start-order-filter",
        )

        first = service.start_task(command)
        replay = service.start_task(command)

        self.assertEqual(first.task.task_id, replay.task.task_id)
        self.assertEqual(first.run.run_id, replay.run.run_id)
        self.assertEqual(first.task.status, TaskStatus.OUTLINE_REVIEW)
        self.assertEqual(first.run.status, RunStatus.WAITING_USER)
        self.assertEqual(len(first.outlines), 1)
        self.assertEqual(model.calls["extract_requirement_brief"], 1)
        self.assertEqual(model.calls["generate_outline"], 1)


if __name__ == "__main__":
    unittest.main()
