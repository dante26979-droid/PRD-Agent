import unittest

from prd_agent.domain.commands import ConfirmOutline, StartTask
from prd_agent.hashing import sha256_json
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.model import StructuredModelResult
from prd_agent.workflow.service import WorkflowService
from prd_agent.workflow.stub_model import HeuristicWorkflowModel


class RecordingModel(HeuristicWorkflowModel):
    def __init__(self):
        self.unit_payload = None

    def complete(self, operation, payload, *, repair=False):
        if operation == "generate_confirmation_unit":
            self.unit_payload = payload
        return super().complete(operation, payload, repair=repair)


class WorkflowInvestigationHookTests(unittest.TestCase):
    def test_unit_generation_receives_only_provider_context(self) -> None:
        model = RecordingModel()
        expected = {
            "investigation_id": "investigation-1",
            "status": "COMPLETE",
            "fact_ids": ["fact-1"],
            "unknown_ids": [],
            "conflict_ids": [],
            "evidence_ids": ["evidence-1"],
        }
        service = WorkflowService(
            InMemoryWorkflowRepository(),
            model,
            unit_context_provider=lambda **_: expected,
        )
        outline = service.start_task(
            StartTask("订单金额允许两位小数", "start-hook")
        )

        service.confirm_outline(
            ConfirmOutline(
                outline.task.task_id,
                outline.current_outline.version,
                outline.task.version,
                "confirm-hook",
            )
        )

        self.assertEqual(model.unit_payload["investigation_context"], expected)


if __name__ == "__main__":
    unittest.main()
