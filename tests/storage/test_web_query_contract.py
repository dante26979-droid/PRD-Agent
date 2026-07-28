from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prd_agent.domain.commands import StartTask
from prd_agent.domain.errors import NotFound
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService
from prd_agent.workflow.stub_model import HeuristicWorkflowModel


def test_owner_scoped_snapshot_list_and_event_cursor():
    repository = InMemoryWorkflowRepository()
    service = WorkflowService(repository, HeuristicWorkflowModel())
    local = service.start_task(StartTask("本地需求", "local"))
    other = service.start_task(
        StartTask("其他需求", "other", actor_id="other-user")
    )

    assert [item.task_id for item in repository.list_tasks("local-user")] == [
        local.task.task_id
    ]
    assert [item.task_id for item in repository.list_tasks("other-user")] == [
        other.task.task_id
    ]
    with pytest.raises(NotFound):
        repository.snapshot_for_owner(other.task.task_id, "local-user")

    events = repository.list_events(
        local.task.task_id,
        "local-user",
        after_sequence=1,
    )
    assert events
    assert all(item.sequence > 1 for item in events)
    assert repository.latest_event_sequence(
        local.task.task_id,
        "local-user",
    ) == events[-1].sequence


def test_task_list_uses_stable_updated_at_and_id_cursor():
    repository = InMemoryWorkflowRepository()
    moment = datetime(2026, 7, 26, tzinfo=timezone.utc)
    for index in range(4):
        service = WorkflowService(repository, HeuristicWorkflowModel())
        snapshot = service.start_task(StartTask(f"需求 {index}", f"start-{index}"))
        task = repository.get_task(snapshot.task.task_id)
        task.updated_at = moment + timedelta(minutes=index // 2)
        repository.save_task(task)

    first = repository.list_tasks("local-user", limit=2)
    second = repository.list_tasks(
        "local-user",
        before=(first[-1].updated_at, first[-1].task_id),
        limit=2,
    )

    ids = [item.task_id for item in first + second]
    assert len(ids) == len(set(ids)) == 4
