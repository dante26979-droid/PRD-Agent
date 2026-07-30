import pytest

from prd_agent.domain.commands import ConfirmOutline, StartTask
from prd_agent.domain.enums import RunStatus, TaskStatus, UnitStatus
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService

from .support import ScriptedModel, first_outline, sufficient_brief


def test_unexpected_investigation_failure_persists_a_terminal_workflow_state():
    repository = InMemoryWorkflowRepository()
    model = ScriptedModel(
        {
            "extract_requirement_brief": [sufficient_brief()],
            "generate_outline": [first_outline()],
        }
    )

    def failing_provider(**_kwargs):
        raise RuntimeError("repository provider unavailable")

    service = WorkflowService(
        repository,
        model,
        unit_context_provider=failing_provider,
    )
    started = service.start_task(
        StartTask(
            message="订单列表增加创建时间筛选",
            idempotency_key="start-before-provider-failure",
        )
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        service.confirm_outline(
            ConfirmOutline(
                task_id=started.task.task_id,
                outline_version=started.current_outline.version,
                expected_task_version=started.task.version,
                idempotency_key="confirm-before-provider-failure",
            )
        )

    failed = repository.snapshot(started.task.task_id)
    unit = failed.current_outline.confirmation_units[0]

    assert failed.task.status == TaskStatus.FAILED
    assert failed.run.status == RunStatus.FAILED
    assert unit.status == UnitStatus.FAILED
    assert repository.checkpoints[failed.run.thread_id]["current_node"] == (
        "GENERATE_FIRST_UNIT_FAILED"
    )
