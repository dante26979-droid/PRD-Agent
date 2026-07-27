from __future__ import annotations

import os
import uuid

import pytest

from prd_agent.domain.commands import StartTask
from prd_agent.domain.errors import NotFound
from prd_agent.storage.postgres import PostgresWorkflowRepository
from prd_agent.workflow.service import WorkflowService
from prd_agent.workflow.stub_model import HeuristicWorkflowModel


pytestmark = pytest.mark.skipif(
    not os.environ.get("PRD_AGENT_TEST_DATABASE_DSN"),
    reason="PRD_AGENT_TEST_DATABASE_DSN is required",
)


def test_postgres_owner_list_snapshot_and_event_cursor():
    repository = PostgresWorkflowRepository.from_dsn(
        os.environ["PRD_AGENT_TEST_DATABASE_DSN"]
    )
    owner_id = f"web-owner-{uuid.uuid4().hex}"
    service = WorkflowService(repository, HeuristicWorkflowModel())
    task_id = None
    try:
        snapshot = service.start_task(
            StartTask(
                "订单列表增加创建时间筛选",
                f"web-start-{uuid.uuid4().hex}",
                actor_id=owner_id,
            )
        )
        task_id = snapshot.task.task_id
        repository.commit()

        listed = repository.list_tasks(owner_id, limit=20)
        restored = repository.snapshot_for_owner(task_id, owner_id)
        events = repository.list_events(
            task_id,
            owner_id,
            after_sequence=1,
        )

        assert [item.task_id for item in listed] == [task_id]
        assert restored.task.owner_id == owner_id
        assert events
        assert all(item.sequence > 1 for item in events)
        assert repository.latest_event_sequence(task_id, owner_id) == events[-1].sequence
        with pytest.raises(NotFound):
            repository.snapshot_for_owner(task_id, "other-user")
    finally:
        if task_id:
            with repository.connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM domain_events WHERE task_id = %s",
                    (task_id,),
                )
                cursor.execute(
                    "DELETE FROM workflow_checkpoints WHERE task_id = %s",
                    (task_id,),
                )
                cursor.execute(
                    "DELETE FROM idempotency_records WHERE task_id = %s",
                    (task_id,),
                )
                cursor.execute(
                    "DELETE FROM agent_runs WHERE task_id = %s",
                    (task_id,),
                )
                cursor.execute(
                    "DELETE FROM outline_versions WHERE task_id = %s",
                    (task_id,),
                )
                cursor.execute(
                    "DELETE FROM requirement_brief_versions WHERE task_id = %s",
                    (task_id,),
                )
                cursor.execute(
                    "DELETE FROM task_messages WHERE task_id = %s",
                    (task_id,),
                )
                cursor.execute(
                    "DELETE FROM prd_tasks WHERE task_id = %s",
                    (task_id,),
                )
            repository.connection.commit()
        repository.connection.close()
