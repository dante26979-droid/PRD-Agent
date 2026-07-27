import unittest

from prd_agent.domain.commands import ReplyToTask, StartTask
from prd_agent.domain.enums import RunStatus, TaskStatus
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService

from .support import ScriptedModel, first_outline, sufficient_brief


class ClarificationTests(unittest.TestCase):
    def test_insufficient_requirement_waits_then_reply_generates_outline(self) -> None:
        incomplete = sufficient_brief()
        incomplete["target_users"] = []
        incomplete["open_questions"] = ["哪些角色使用该筛选？"]
        model = ScriptedModel(
            {
                "extract_requirement_brief": [incomplete, sufficient_brief()],
                "generate_outline": [first_outline()],
            }
        )
        service = WorkflowService(InMemoryWorkflowRepository(), model)

        waiting = service.start_task(StartTask("优化订单体验", "start-clarify"))

        self.assertEqual(waiting.task.status, TaskStatus.CLARIFYING)
        self.assertEqual(waiting.run.status, RunStatus.WAITING_USER)
        self.assertEqual(waiting.current_brief.brief.open_questions, ("哪些角色使用该筛选？",))
        self.assertEqual(waiting.outlines, ())

        resumed = service.reply_to_task(
            ReplyToTask(
                task_id=waiting.task.task_id,
                message="订单运营使用，需要按创建时间筛选",
                expected_task_version=waiting.task.version,
                idempotency_key="reply-clarify",
            )
        )

        self.assertEqual(resumed.task.status, TaskStatus.OUTLINE_REVIEW)
        self.assertEqual(resumed.run.status, RunStatus.WAITING_USER)
        self.assertEqual(resumed.run.thread_id, waiting.run.thread_id)
        self.assertNotEqual(resumed.run.run_id, waiting.run.run_id)
        self.assertEqual(len(resumed.briefs), 2)
        self.assertEqual(len(resumed.outlines), 1)


if __name__ == "__main__":
    unittest.main()
