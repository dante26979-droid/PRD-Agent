from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

from prd_agent.production.dispatch import (
    InMemoryProductionControlStore,
    LostLeaseError,
    OutboxPublisher,
    ProductionRunStatus,
    RunExecutor,
    RunReconciler,
)
import pytest


class RecordingBroker:
    def __init__(self) -> None:
        self.commands = []
        self.lose_first_ack = True

    def publish(self, command) -> None:
        self.commands.append(command)
        if self.lose_first_ack:
            self.lose_first_ack = False
            raise TimeoutError("broker accepted the message but response was lost")


def test_duplicate_outbox_delivery_executes_one_logical_run():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    store = InMemoryProductionControlStore()
    outbox = store.create_run_dispatch(
        run_id="run-1",
        task_id="task-1",
        owner_id="alice",
        reason="START_OR_RESUME",
        now=now,
    )
    broker = RecordingBroker()
    publisher = OutboxPublisher(
        store,
        broker,
        publisher_id="publisher-1",
        lease_ttl=timedelta(seconds=10),
    )

    assert publisher.publish_once(now=now) == 0
    assert store.get_outbox(outbox.message_id).published_at is None
    assert publisher.publish_once(now=now + timedelta(seconds=11)) == 1
    assert len(broker.commands) == 2

    calls = []
    executor = RunExecutor(
        store,
        worker_id="agent-worker-1",
        handler=lambda context: calls.append(context.run_id),
        lease_ttl=timedelta(seconds=30),
    )
    results = [
        executor.execute(command, now=now + timedelta(seconds=12))
        for command in broker.commands
    ]

    assert results == ["SUCCEEDED", "SUCCEEDED"]
    assert calls == ["run-1"]
    assert store.get_run("run-1").status == "SUCCEEDED"


def test_persisted_cancellation_stops_a_redelivered_run_without_handler_call():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    store = InMemoryProductionControlStore()
    outbox = store.create_run_dispatch(
        run_id="run-stop",
        task_id="task-stop",
        owner_id="alice",
        reason="START_OR_RESUME",
        now=now,
    )
    stopped = store.request_cancellation(
        "run-stop",
        owner_id="alice",
        now=now + timedelta(seconds=1),
    )
    calls = []
    executor = RunExecutor(
        store,
        worker_id="agent-worker-1",
        handler=lambda context: calls.append(context.run_id),
    )

    result = executor.execute(
        outbox.command,
        now=now + timedelta(seconds=2),
    )

    assert stopped.status == ProductionRunStatus.STOPPING
    assert result == "STOPPED"
    assert calls == []
    assert store.get_run("run-stop").status == ProductionRunStatus.STOPPED


def test_heartbeat_extends_only_the_current_worker_lease():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    store = InMemoryProductionControlStore()
    store.create_run_dispatch(
        run_id="run-heartbeat",
        task_id="task-heartbeat",
        owner_id="alice",
        reason="START_OR_RESUME",
        now=now,
    )
    grant = store.acquire_run(
        "run-heartbeat",
        worker_id="worker-a",
        now=now,
        lease_ttl=timedelta(seconds=10),
    )

    renewed = store.heartbeat(
        grant,
        now=now + timedelta(seconds=5),
        lease_ttl=timedelta(seconds=20),
    )

    assert renewed.expires_at == now + timedelta(seconds=25)
    assert store.get_run("run-heartbeat").attempt_count == 1


def test_expired_worker_is_fenced_after_another_worker_takes_over():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    store = InMemoryProductionControlStore()
    store.create_run_dispatch(
        run_id="run-fenced",
        task_id="task-fenced",
        owner_id="alice",
        reason="START_OR_RESUME",
        now=now,
    )
    stale = store.acquire_run(
        "run-fenced",
        worker_id="worker-a",
        now=now,
        lease_ttl=timedelta(seconds=10),
    )
    current = store.acquire_run(
        "run-fenced",
        worker_id="worker-b",
        now=now + timedelta(seconds=11),
        lease_ttl=timedelta(seconds=10),
    )

    result = store.complete_run(current, ProductionRunStatus.SUCCEEDED)

    assert current.fencing_token == stale.fencing_token + 1
    assert result.status == ProductionRunStatus.SUCCEEDED
    with pytest.raises(LostLeaseError):
        store.complete_run(stale, ProductionRunStatus.FAILED)


def test_run_executor_heartbeats_while_a_long_handler_is_blocked():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)

    class ObservableHeartbeatStore(InMemoryProductionControlStore):
        def __init__(self):
            super().__init__()
            self.heartbeat_observed = Event()

        def heartbeat(self, grant, *, now, lease_ttl):
            renewed = super().heartbeat(
                grant,
                now=now,
                lease_ttl=lease_ttl,
            )
            self.heartbeat_observed.set()
            return renewed

    store = ObservableHeartbeatStore()
    outbox = store.create_run_dispatch(
        run_id="run-long-handler",
        task_id="task-long-handler",
        owner_id="alice",
        reason="START_OR_RESUME",
        now=now,
    )
    handler_started = Event()
    release_handler = Event()
    contexts = []

    def blocking_handler(context):
        contexts.append(context)
        handler_started.set()
        assert release_handler.wait(timeout=2)

    executor = RunExecutor(
        store,
        worker_id="worker-a",
        handler=blocking_handler,
        lease_ttl=timedelta(milliseconds=90),
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        execution = pool.submit(executor.execute, outbox.command, now=now)
        assert handler_started.wait(timeout=2)
        assert store.heartbeat_observed.wait(timeout=2)
        contender = store.acquire_run(
            "run-long-handler",
            worker_id="worker-b",
            now=now + timedelta(milliseconds=91),
            lease_ttl=timedelta(milliseconds=90),
        )
        release_handler.set()
        result = execution.result(timeout=2)

    assert contender is None
    assert contexts[0].fencing_token == 1
    assert result == "SUCCEEDED"


def test_reconciler_enqueues_one_recovery_wakeup_for_an_expired_run():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    store = InMemoryProductionControlStore()
    store.create_run_dispatch(
        run_id="run-recover",
        task_id="task-recover",
        owner_id="alice",
        reason="START_OR_RESUME",
        now=now,
    )
    store.acquire_run(
        "run-recover",
        worker_id="dead-worker",
        now=now,
        lease_ttl=timedelta(seconds=10),
    )
    reconciler = RunReconciler(store, grace_period=timedelta(seconds=5))

    first = reconciler.reconcile(now=now + timedelta(seconds=16))
    second = reconciler.reconcile(now=now + timedelta(seconds=17))

    assert len(first) == 1
    assert first[0].command.reason == "RECOVER"
    assert second == ()


def test_quarantined_recovery_does_not_create_an_unbounded_recovery_loop():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    store = InMemoryProductionControlStore()
    store.create_run_dispatch(
        run_id="run-quarantined-recovery",
        task_id="task-quarantined-recovery",
        owner_id="alice",
        reason="START_OR_RESUME",
        now=now,
    )
    store.acquire_run(
        "run-quarantined-recovery",
        worker_id="dead-worker",
        now=now,
        lease_ttl=timedelta(seconds=10),
    )
    reconciler = RunReconciler(store, grace_period=timedelta(seconds=5))
    recovery = reconciler.reconcile(now=now + timedelta(seconds=16))[0]
    store.claim_outbox(
        publisher_id="publisher-a",
        now=now + timedelta(seconds=16),
        lease_ttl=timedelta(seconds=5),
        limit=10,
    )
    store.mark_outbox_failed(
        recovery.message_id,
        publisher_id="publisher-a",
        now=now + timedelta(seconds=17),
        max_attempts=1,
    )

    repeated = reconciler.reconcile(now=now + timedelta(seconds=30))

    assert repeated == ()


def test_retry_creates_a_new_run_and_preserves_the_failed_attempt():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    store = InMemoryProductionControlStore()
    store.create_run_dispatch(
        run_id="run-failed",
        task_id="task-retry",
        owner_id="alice",
        reason="START_OR_RESUME",
        now=now,
    )
    grant = store.acquire_run(
        "run-failed",
        worker_id="worker-a",
        now=now,
        lease_ttl=timedelta(seconds=10),
    )
    store.complete_run(grant, ProductionRunStatus.FAILED)

    retried = store.retry_run(
        "run-failed",
        owner_id="alice",
        now=now + timedelta(seconds=1),
        idempotency_key="retry-failed-run",
        request_hash="sha256:retry-request",
    )
    replay = store.retry_run(
        "run-failed",
        owner_id="alice",
        now=now + timedelta(seconds=2),
        idempotency_key="retry-failed-run",
        request_hash="sha256:retry-request",
    )

    assert retried.run_id != "run-failed"
    assert replay.run_id == retried.run_id
    assert retried.status == ProductionRunStatus.QUEUED
    assert store.get_run("run-failed").status == ProductionRunStatus.FAILED


def test_retry_idempotency_key_rejects_different_input():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    store = InMemoryProductionControlStore()
    for run_id in ("run-failed-a", "run-failed-b"):
        store.create_run_dispatch(
            run_id=run_id,
            task_id="task-retry",
            owner_id="alice",
            reason="START_OR_RESUME",
            now=now,
        )
        grant = store.acquire_run(
            run_id,
            worker_id=f"worker-{run_id}",
            now=now,
            lease_ttl=timedelta(seconds=10),
        )
        store.complete_run(grant, ProductionRunStatus.FAILED)

    store.retry_run(
        "run-failed-a",
        owner_id="alice",
        now=now + timedelta(seconds=1),
        idempotency_key="shared-retry-key",
        request_hash="sha256:request-a",
    )

    with pytest.raises(ValueError, match="idempotency"):
        store.retry_run(
            "run-failed-b",
            owner_id="alice",
            now=now + timedelta(seconds=2),
            idempotency_key="shared-retry-key",
            request_hash="sha256:request-b",
        )
