from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from threading import RLock
from typing import Callable, Protocol
import uuid


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProductionRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


TERMINAL_RUN_STATUSES = frozenset(
    {
        ProductionRunStatus.SUCCEEDED,
        ProductionRunStatus.FAILED,
        ProductionRunStatus.STOPPED,
    }
)


@dataclass(frozen=True)
class RunCommand:
    message_id: str
    payload_version: int
    run_id: str
    reason: str


@dataclass(frozen=True)
class OutboxMessage:
    message_id: str
    aggregate_id: str
    aggregate_version: int
    topic: str
    payload_version: int
    command: RunCommand
    available_at: datetime
    attempts: int = 0
    published_at: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    quarantined_at: datetime | None = None


@dataclass(frozen=True)
class ProductionRun:
    run_id: str
    task_id: str
    owner_id: str
    status: ProductionRunStatus
    fencing_token: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    cancellation_requested_at: datetime | None = None


@dataclass(frozen=True)
class LeaseGrant:
    run_id: str
    worker_id: str
    fencing_token: int
    expires_at: datetime


class MessageBroker(Protocol):
    def publish(self, command: RunCommand) -> None: ...


class LostLeaseError(RuntimeError):
    pass


class InMemoryProductionControlStore:
    """Thread-safe reference implementation of the production control contract."""

    def __init__(self) -> None:
        self._runs: dict[str, ProductionRun] = {}
        self._outbox: dict[str, OutboxMessage] = {}
        self._inbox: set[tuple[str, str]] = set()
        self._lock = RLock()

    def create_run_dispatch(
        self,
        *,
        run_id: str,
        task_id: str,
        owner_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> OutboxMessage:
        current_time = now or _now()
        with self._lock:
            existing = self._runs.get(run_id)
            if existing is not None:
                return deepcopy(
                    next(
                        value
                        for value in self._outbox.values()
                        if value.aggregate_id == run_id
                    )
                )
            message_id = f"outbox-{uuid.uuid4().hex}"
            command = RunCommand(
                message_id=message_id,
                payload_version=1,
                run_id=run_id,
                reason=reason,
            )
            outbox = OutboxMessage(
                message_id=message_id,
                aggregate_id=run_id,
                aggregate_version=1,
                topic="agent.run",
                payload_version=1,
                command=command,
                available_at=current_time,
            )
            self._runs[run_id] = ProductionRun(
                run_id=run_id,
                task_id=task_id,
                owner_id=owner_id,
                status=ProductionRunStatus.QUEUED,
            )
            self._outbox[message_id] = outbox
            return deepcopy(outbox)

    def get_outbox(self, message_id: str) -> OutboxMessage:
        with self._lock:
            return deepcopy(self._outbox[message_id])

    def claim_outbox(
        self,
        *,
        publisher_id: str,
        now: datetime,
        lease_ttl: timedelta,
        limit: int,
    ) -> tuple[OutboxMessage, ...]:
        claimed = []
        with self._lock:
            candidates = sorted(
                self._outbox.values(),
                key=lambda item: (item.available_at, item.message_id),
            )
            for value in candidates:
                if len(claimed) >= limit:
                    break
                if (
                    value.published_at is not None
                    or value.quarantined_at is not None
                    or value.available_at > now
                    or (
                        value.lease_expires_at is not None
                        and value.lease_expires_at > now
                    )
                ):
                    continue
                updated = replace(
                    value,
                    lease_owner=publisher_id,
                    lease_expires_at=now + lease_ttl,
                )
                self._outbox[value.message_id] = updated
                claimed.append(deepcopy(updated))
        return tuple(claimed)

    def mark_outbox_published(
        self,
        message_id: str,
        *,
        publisher_id: str,
        now: datetime,
    ) -> bool:
        with self._lock:
            value = self._outbox[message_id]
            if value.lease_owner != publisher_id:
                return False
            self._outbox[message_id] = replace(
                value,
                published_at=now,
                lease_owner=None,
                lease_expires_at=None,
            )
            return True

    def mark_outbox_failed(
        self,
        message_id: str,
        *,
        publisher_id: str,
        now: datetime,
        max_attempts: int,
    ) -> bool:
        with self._lock:
            value = self._outbox[message_id]
            if value.lease_owner != publisher_id:
                return False
            attempts = value.attempts + 1
            quarantine = now if attempts >= max_attempts else None
            self._outbox[message_id] = replace(
                value,
                attempts=attempts,
                available_at=now + timedelta(seconds=min(2 ** attempts, 300)),
                quarantined_at=quarantine,
                lease_owner=None,
                lease_expires_at=None,
            )
            return True

    def get_run(self, run_id: str) -> ProductionRun:
        with self._lock:
            return deepcopy(self._runs[run_id])

    def list_runs(self, task_id: str, owner_id: str) -> tuple[ProductionRun, ...]:
        with self._lock:
            return tuple(
                deepcopy(value)
                for value in sorted(
                    self._runs.values(),
                    key=lambda item: item.run_id,
                    reverse=True,
                )
                if value.task_id == task_id and value.owner_id == owner_id
            )

    def request_cancellation(
        self,
        run_id: str,
        *,
        owner_id: str,
        now: datetime,
    ) -> ProductionRun:
        with self._lock:
            run = self._runs[run_id]
            if run.owner_id != owner_id:
                raise PermissionError("run does not belong to the requesting owner")
            if run.status in TERMINAL_RUN_STATUSES:
                return deepcopy(run)
            updated = replace(
                run,
                status=ProductionRunStatus.STOPPING,
                cancellation_requested_at=run.cancellation_requested_at or now,
            )
            self._runs[run_id] = updated
            return deepcopy(updated)

    def retry_run(
        self,
        run_id: str,
        *,
        owner_id: str,
        now: datetime,
    ) -> ProductionRun:
        with self._lock:
            previous = self._runs[run_id]
            if previous.owner_id != owner_id:
                raise KeyError(run_id)
            if previous.status not in {
                ProductionRunStatus.FAILED,
                ProductionRunStatus.STOPPED,
            }:
                raise ValueError("only failed or stopped runs can be retried")
            new_run_id = f"run-{uuid.uuid4().hex}"
            self.create_run_dispatch(
                run_id=new_run_id,
                task_id=previous.task_id,
                owner_id=owner_id,
                reason="RETRY",
                now=now,
            )
            return deepcopy(self._runs[new_run_id])

    def acquire_run(
        self,
        run_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_ttl: timedelta,
    ) -> LeaseGrant | None:
        with self._lock:
            run = self._runs[run_id]
            if run.status in TERMINAL_RUN_STATUSES:
                return None
            if (
                run.lease_owner is not None
                and run.lease_owner != worker_id
                and run.lease_expires_at is not None
                and run.lease_expires_at > now
            ):
                return None
            token = run.fencing_token + 1
            expires_at = now + lease_ttl
            self._runs[run_id] = replace(
                run,
                status=(
                    ProductionRunStatus.STOPPING
                    if run.cancellation_requested_at is not None
                    else ProductionRunStatus.RUNNING
                ),
                fencing_token=token,
                lease_owner=worker_id,
                lease_expires_at=expires_at,
                attempt_count=run.attempt_count + 1,
            )
            return LeaseGrant(run_id, worker_id, token, expires_at)

    def complete_run(
        self,
        grant: LeaseGrant,
        status: ProductionRunStatus,
    ) -> ProductionRun:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError("only terminal statuses can complete a run")
        with self._lock:
            run = self._runs[grant.run_id]
            if (
                run.lease_owner != grant.worker_id
                or run.fencing_token != grant.fencing_token
            ):
                raise LostLeaseError("worker lease is no longer current")
            updated = replace(
                run,
                status=status,
                lease_owner=None,
                lease_expires_at=None,
            )
            self._runs[grant.run_id] = updated
            return deepcopy(updated)

    def heartbeat(
        self,
        grant: LeaseGrant,
        *,
        now: datetime,
        lease_ttl: timedelta,
    ) -> LeaseGrant:
        with self._lock:
            run = self._runs[grant.run_id]
            if (
                run.lease_owner != grant.worker_id
                or run.fencing_token != grant.fencing_token
                or run.lease_expires_at is None
                or run.lease_expires_at <= now
            ):
                raise LostLeaseError("worker lease is no longer current")
            expires_at = now + lease_ttl
            self._runs[grant.run_id] = replace(
                run,
                lease_expires_at=expires_at,
            )
            return LeaseGrant(
                grant.run_id,
                grant.worker_id,
                grant.fencing_token,
                expires_at,
            )

    def record_inbox(self, consumer_name: str, message_id: str) -> None:
        with self._lock:
            self._inbox.add((consumer_name, message_id))

    def has_inbox(self, consumer_name: str, message_id: str) -> bool:
        with self._lock:
            return (consumer_name, message_id) in self._inbox

    def enqueue_recoveries(
        self,
        *,
        now: datetime,
        grace_period: timedelta,
        limit: int,
    ) -> tuple[OutboxMessage, ...]:
        created: list[OutboxMessage] = []
        with self._lock:
            candidates = sorted(self._runs.values(), key=lambda item: item.run_id)
            for run in candidates:
                if len(created) >= limit:
                    break
                if (
                    run.status not in {
                        ProductionRunStatus.RUNNING,
                        ProductionRunStatus.STOPPING,
                    }
                    or run.lease_expires_at is None
                    or run.lease_expires_at + grace_period > now
                ):
                    continue
                has_recovery = any(
                    item.aggregate_id == run.run_id
                    and item.command.reason == "RECOVER"
                    and item.published_at is None
                    and item.quarantined_at is None
                    for item in self._outbox.values()
                )
                if has_recovery:
                    continue
                aggregate_version = (
                    max(
                        (
                            item.aggregate_version
                            for item in self._outbox.values()
                            if item.aggregate_id == run.run_id
                        ),
                        default=0,
                    )
                    + 1
                )
                message_id = f"outbox-{uuid.uuid4().hex}"
                command = RunCommand(
                    message_id=message_id,
                    payload_version=1,
                    run_id=run.run_id,
                    reason="RECOVER",
                )
                message = OutboxMessage(
                    message_id=message_id,
                    aggregate_id=run.run_id,
                    aggregate_version=aggregate_version,
                    topic="agent.run",
                    payload_version=1,
                    command=command,
                    available_at=now,
                )
                self._outbox[message_id] = message
                created.append(deepcopy(message))
        return tuple(created)


class OutboxPublisher:
    def __init__(
        self,
        store,
        broker: MessageBroker,
        *,
        publisher_id: str,
        lease_ttl: timedelta = timedelta(seconds=30),
        max_attempts: int = 8,
    ) -> None:
        self.store = store
        self.broker = broker
        self.publisher_id = publisher_id
        self.lease_ttl = lease_ttl
        self.max_attempts = max_attempts

    def publish_once(self, *, now: datetime | None = None, limit: int = 100) -> int:
        current_time = now or _now()
        published = 0
        for message in self.store.claim_outbox(
            publisher_id=self.publisher_id,
            now=current_time,
            lease_ttl=self.lease_ttl,
            limit=limit,
        ):
            try:
                self.broker.publish(message.command)
            except Exception:
                self.store.mark_outbox_failed(
                    message.message_id,
                    publisher_id=self.publisher_id,
                    now=current_time,
                    max_attempts=self.max_attempts,
                )
                continue
            if self.store.mark_outbox_published(
                message.message_id,
                publisher_id=self.publisher_id,
                now=current_time,
            ):
                published += 1
        return published


class RunReconciler:
    def __init__(
        self,
        store,
        *,
        grace_period: timedelta = timedelta(seconds=15),
    ) -> None:
        self.store = store
        self.grace_period = grace_period

    def reconcile(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> tuple[OutboxMessage, ...]:
        return self.store.enqueue_recoveries(
            now=now or _now(),
            grace_period=self.grace_period,
            limit=limit,
        )


class RunExecutor:
    def __init__(
        self,
        store,
        *,
        worker_id: str,
        handler: Callable[[str], None],
        lease_ttl: timedelta = timedelta(seconds=60),
        consumer_name: str = "agent-worker",
    ) -> None:
        self.store = store
        self.worker_id = worker_id
        self.handler = handler
        self.lease_ttl = lease_ttl
        self.consumer_name = consumer_name

    def execute(self, command: RunCommand, *, now: datetime | None = None) -> str:
        if command.payload_version != 1:
            raise ValueError("unsupported run command payload version")
        current = self.store.get_run(command.run_id)
        if current.status in TERMINAL_RUN_STATUSES:
            self.store.record_inbox(self.consumer_name, command.message_id)
            return current.status.value
        grant = self.store.acquire_run(
            command.run_id,
            worker_id=self.worker_id,
            now=now or _now(),
            lease_ttl=self.lease_ttl,
        )
        if grant is None:
            return self.store.get_run(command.run_id).status.value
        acquired = self.store.get_run(command.run_id)
        if acquired.cancellation_requested_at is not None:
            result = self.store.complete_run(
                grant,
                ProductionRunStatus.STOPPED,
            )
            self.store.record_inbox(self.consumer_name, command.message_id)
            return result.status.value
        try:
            self.handler(command.run_id)
        except Exception:
            result = self.store.complete_run(
                grant,
                ProductionRunStatus.FAILED,
            )
            self.store.record_inbox(self.consumer_name, command.message_id)
            return result.status.value
        result = self.store.complete_run(
            grant,
            ProductionRunStatus.SUCCEEDED,
        )
        self.store.record_inbox(self.consumer_name, command.message_id)
        return result.status.value
