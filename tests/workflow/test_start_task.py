from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
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

    def test_concurrent_start_with_same_key_invokes_the_model_once(self) -> None:
        class BlockingModel(ScriptedModel):
            def __init__(self):
                super().__init__(
                    {
                        "extract_requirement_brief": [
                            sufficient_brief(),
                            sufficient_brief(),
                        ],
                        "generate_outline": [
                            first_outline(),
                            first_outline(),
                        ],
                    }
                )
                self.started = Event()
                self.release = Event()
                self.duplicate_call = Event()
                self._count = 0
                self._count_lock = Lock()

            def complete(self, operation, payload, *, repair=False):
                if operation == "extract_requirement_brief":
                    with self._count_lock:
                        self._count += 1
                        count = self._count
                    if count == 1:
                        self.started.set()
                        self.release.wait(timeout=2)
                    else:
                        self.duplicate_call.set()
                return super().complete(
                    operation,
                    payload,
                    repair=repair,
                )

        model = BlockingModel()
        service = WorkflowService(InMemoryWorkflowRepository(), model)
        command = StartTask(
            message="订单列表增加创建时间筛选",
            idempotency_key="concurrent-start",
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(service.start_task, command)
            self.assertTrue(model.started.wait(timeout=2))
            second_future = executor.submit(service.start_task, command)
            duplicate = model.duplicate_call.wait(timeout=0.2)
            model.release.set()
            first = first_future.result(timeout=2)
            second = second_future.result(timeout=2)

        self.assertFalse(duplicate)
        self.assertEqual(first.task.task_id, second.task.task_id)
        self.assertEqual(model.calls["extract_requirement_brief"], 1)


if __name__ == "__main__":
    unittest.main()
