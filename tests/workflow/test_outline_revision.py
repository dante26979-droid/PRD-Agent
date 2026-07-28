import unittest

from prd_agent.domain.commands import ReplyToTask, StartTask
from prd_agent.domain.enums import OutlineStatus, TaskStatus
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService

from .support import ScriptedModel, first_outline, sufficient_brief


class OutlineRevisionTests(unittest.TestCase):
    def test_reply_during_outline_review_supersedes_instead_of_mutating_version(self) -> None:
        revised_outline = first_outline()
        revised_outline["title"] = "仅筛选新订单的创建时间"
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief(), sufficient_brief()],
                "generate_outline": [first_outline(), revised_outline],
            }
        )
        service = WorkflowService(InMemoryWorkflowRepository(), model)
        first = service.start_task(StartTask("订单列表增加创建时间筛选", "start"))

        revised = service.reply_to_task(
            ReplyToTask(
                first.task.task_id,
                "调整大纲：仅影响新订单",
                first.task.version,
                "revise-outline",
            )
        )

        self.assertEqual(revised.task.status, TaskStatus.OUTLINE_REVIEW)
        self.assertEqual(len(revised.outlines), 2)
        self.assertEqual(revised.outlines[0].status, OutlineStatus.SUPERSEDED)
        self.assertEqual(revised.outlines[1].status, OutlineStatus.PENDING_CONFIRMATION)
        self.assertEqual(revised.outlines[1].version, 2)
        self.assertEqual(revised.task.current_outline_version, 2)


if __name__ == "__main__":
    unittest.main()
