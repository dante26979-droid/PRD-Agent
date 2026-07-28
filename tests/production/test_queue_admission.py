from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prd_agent.production.dispatch import (
    InMemoryProductionControlStore,
    ProductionRunStatus,
    QueuePolicy,
)


def test_run_created_over_capacity_waits_without_a_queue_slot():
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    store = InMemoryProductionControlStore(
        queue_policy=QueuePolicy(max_global_runnable=1, max_runnable_per_owner=1)
    )

    store.create_run_dispatch(
        run_id="run-a",
        task_id="task-a",
        owner_id="alice",
        reason="START_OR_RESUME",
        now=now,
    )
    store.create_run_dispatch(
        run_id="run-b",
        task_id="task-b",
        owner_id="bob",
        reason="START_OR_RESUME",
        now=now,
    )

    waiting = store.get_run("run-b")
    assert waiting.status is ProductionRunStatus.WAITING_CAPACITY
    assert waiting.queue_slot_acquired is False
    assert store.acquire_run(
        "run-b",
        worker_id="agent-1",
        now=now,
        lease_ttl=timedelta(seconds=30),
    ) is None


def test_capacity_is_promoted_fairly_after_the_current_run_completes():
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    store = InMemoryProductionControlStore(
        queue_policy=QueuePolicy(max_global_runnable=1, max_runnable_per_owner=1)
    )
    store.create_run_dispatch(
        run_id="run-alice-1",
        task_id="task-alice-1",
        owner_id="alice",
        reason="START_OR_RESUME",
        now=now,
    )
    store.create_run_dispatch(
        run_id="run-alice-2",
        task_id="task-alice-2",
        owner_id="alice",
        reason="START_OR_RESUME",
        now=now,
    )
    store.create_run_dispatch(
        run_id="run-bob-1",
        task_id="task-bob-1",
        owner_id="bob",
        reason="START_OR_RESUME",
        now=now,
    )

    grant = store.acquire_run(
        "run-alice-1",
        worker_id="agent-1",
        now=now,
        lease_ttl=timedelta(seconds=30),
    )
    assert grant is not None
    store.complete_run(grant, ProductionRunStatus.SUCCEEDED)

    assert store.get_run("run-bob-1").status is ProductionRunStatus.QUEUED
    assert store.get_run("run-alice-2").status is ProductionRunStatus.WAITING_CAPACITY
