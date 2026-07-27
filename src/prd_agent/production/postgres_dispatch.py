"""PostgreSQL implementation of the production dispatch control contract."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import uuid

from prd_agent.production.dispatch import (
    LeaseGrant,
    LostLeaseError,
    OutboxMessage,
    ProductionRun,
    ProductionRunStatus,
    RunCommand,
    TERMINAL_RUN_STATUSES,
)


class PostgresProductionControlStore:
    """Short-transaction store for Outbox, Inbox, leases, and fencing."""

    def __init__(self, connection) -> None:
        self.connection = connection

    @classmethod
    def from_dsn(cls, dsn: str) -> "PostgresProductionControlStore":
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "PostgreSQL production support requires: "
                "python -m pip install '.[production]'"
            ) from exc
        return cls(psycopg.connect(dsn))

    def create_run_dispatch(
        self,
        *,
        run_id: str,
        task_id: str,
        owner_id: str,
        reason: str,
        now: datetime,
        tenant_id: str = "local",
    ) -> OutboxMessage:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO production_run_control (
                        run_id, task_id, tenant_id, owner_id, status,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, 'QUEUED', %s, %s)
                    ON CONFLICT (run_id) DO NOTHING
                    RETURNING run_id
                    """,
                    (run_id, task_id, tenant_id, owner_id, now, now),
                )
                created = cursor.fetchone() is not None
                if created:
                    message_id = f"outbox-{uuid.uuid4().hex}"
                    payload = {
                        "message_id": message_id,
                        "payload_version": 1,
                        "run_id": run_id,
                        "reason": reason,
                    }
                    cursor.execute(
                        """
                        INSERT INTO outbox_messages (
                            message_id, aggregate_type, aggregate_id,
                            aggregate_version, topic, payload_version,
                            payload, available_at
                        ) VALUES (%s, 'RUN', %s, 1, 'agent.run', 1, %s::jsonb, %s)
                        """,
                        (
                            message_id,
                            run_id,
                            json.dumps(payload, separators=(",", ":")),
                            now,
                        ),
                    )
                cursor.execute(self._OUTBOX_SELECT + " WHERE aggregate_id = %s", (run_id,))
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("run exists without its dispatch outbox")
        return self._outbox_from_row(row)

    def get_outbox(self, message_id: str) -> OutboxMessage:
        with self.connection.cursor() as cursor:
            cursor.execute(self._OUTBOX_SELECT + " WHERE message_id = %s", (message_id,))
            row = cursor.fetchone()
        if row is None:
            raise KeyError(message_id)
        return self._outbox_from_row(row)

    def claim_outbox(
        self,
        *,
        publisher_id: str,
        now: datetime,
        lease_ttl: timedelta,
        limit: int,
    ) -> tuple[OutboxMessage, ...]:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH candidates AS (
                        SELECT message_id
                          FROM outbox_messages
                         WHERE published_at IS NULL
                           AND quarantined_at IS NULL
                           AND available_at <= %s
                           AND (
                               lease_expires_at IS NULL
                               OR lease_expires_at <= %s
                           )
                         ORDER BY available_at, message_id
                         FOR UPDATE SKIP LOCKED
                         LIMIT %s
                    )
                    UPDATE outbox_messages AS message
                       SET lease_owner = %s,
                           lease_expires_at = %s
                      FROM candidates
                     WHERE message.message_id = candidates.message_id
                    RETURNING message.message_id, message.aggregate_id,
                              message.aggregate_version, message.topic,
                              message.payload_version, message.payload,
                              message.available_at, message.attempts,
                              message.published_at, message.lease_owner,
                              message.lease_expires_at, message.quarantined_at
                    """,
                    (now, now, limit, publisher_id, now + lease_ttl),
                )
                rows = cursor.fetchall()
        return tuple(self._outbox_from_row(row) for row in rows)

    def mark_outbox_published(
        self,
        message_id: str,
        *,
        publisher_id: str,
        now: datetime,
    ) -> bool:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE outbox_messages
                       SET published_at = %s,
                           lease_owner = NULL,
                           lease_expires_at = NULL
                     WHERE message_id = %s
                       AND lease_owner = %s
                       AND published_at IS NULL
                    """,
                    (now, message_id, publisher_id),
                )
                return cursor.rowcount == 1

    def mark_outbox_failed(
        self,
        message_id: str,
        *,
        publisher_id: str,
        now: datetime,
        max_attempts: int,
    ) -> bool:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE outbox_messages
                       SET attempts = attempts + 1,
                           available_at = %s + (
                               LEAST(POWER(2, attempts + 1), 300) * INTERVAL '1 second'
                           ),
                           quarantined_at = CASE
                               WHEN attempts + 1 >= %s THEN %s
                               ELSE NULL
                           END,
                           lease_owner = NULL,
                           lease_expires_at = NULL
                     WHERE message_id = %s
                       AND lease_owner = %s
                       AND published_at IS NULL
                    """,
                    (now, max_attempts, now, message_id, publisher_id),
                )
                return cursor.rowcount == 1

    def get_run(self, run_id: str) -> ProductionRun:
        with self.connection.cursor() as cursor:
            cursor.execute(self._RUN_SELECT + " WHERE run_id = %s", (run_id,))
            row = cursor.fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_from_row(row)

    def list_runs(self, task_id: str, owner_id: str) -> tuple[ProductionRun, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                self._RUN_SELECT
                + " WHERE task_id = %s AND owner_id = %s ORDER BY run_id DESC",
                (task_id, owner_id),
            )
            rows = cursor.fetchall()
        return tuple(self._run_from_row(row) for row in rows)

    def request_cancellation(
        self,
        run_id: str,
        *,
        owner_id: str,
        now: datetime,
    ) -> ProductionRun:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE production_run_control
                       SET status = 'STOPPING',
                           cancellation_requested_at =
                               COALESCE(cancellation_requested_at, %s),
                           updated_at = %s
                     WHERE run_id = %s
                       AND owner_id = %s
                       AND status NOT IN ('SUCCEEDED', 'FAILED', 'STOPPED')
                    RETURNING run_id, task_id, owner_id, status, fencing_token,
                              lease_owner, lease_expires_at, attempt_count,
                              cancellation_requested_at
                    """,
                    (now, now, run_id, owner_id),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        self._RUN_SELECT + " WHERE run_id = %s AND owner_id = %s",
                        (run_id, owner_id),
                    )
                    row = cursor.fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_from_row(row)

    def retry_run(
        self,
        run_id: str,
        *,
        owner_id: str,
        now: datetime,
    ) -> ProductionRun:
        previous = self.get_run(run_id)
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
        return self.get_run(new_run_id)

    def acquire_run(
        self,
        run_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_ttl: timedelta,
    ) -> LeaseGrant | None:
        terminal_values = tuple(value.value for value in TERMINAL_RUN_STATUSES)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE production_run_control
                       SET status = CASE
                               WHEN cancellation_requested_at IS NOT NULL
                               THEN 'STOPPING'
                               ELSE 'RUNNING'
                           END,
                           fencing_token = fencing_token + 1,
                           lease_owner = %s,
                           lease_expires_at = %s,
                           attempt_count = attempt_count + 1,
                           updated_at = %s
                     WHERE run_id = %s
                       AND status NOT IN (%s, %s, %s)
                       AND (
                           lease_owner IS NULL
                           OR lease_expires_at IS NULL
                           OR lease_expires_at <= %s
                       )
                    RETURNING fencing_token, lease_expires_at
                    """,
                    (
                        worker_id,
                        now + lease_ttl,
                        now,
                        run_id,
                        *terminal_values,
                        now,
                    ),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return LeaseGrant(run_id, worker_id, row[0], row[1])

    def heartbeat(
        self,
        grant: LeaseGrant,
        *,
        now: datetime,
        lease_ttl: timedelta,
    ) -> LeaseGrant:
        expires_at = now + lease_ttl
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE production_run_control
                       SET lease_expires_at = %s, updated_at = %s
                     WHERE run_id = %s
                       AND lease_owner = %s
                       AND fencing_token = %s
                       AND lease_expires_at > %s
                    """,
                    (
                        expires_at,
                        now,
                        grant.run_id,
                        grant.worker_id,
                        grant.fencing_token,
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LostLeaseError("worker lease is no longer current")
        return LeaseGrant(
            grant.run_id,
            grant.worker_id,
            grant.fencing_token,
            expires_at,
        )

    def complete_run(
        self,
        grant: LeaseGrant,
        status: ProductionRunStatus,
    ) -> ProductionRun:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError("only terminal statuses can complete a run")
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE production_run_control
                       SET status = %s,
                           lease_owner = NULL,
                           lease_expires_at = NULL,
                           updated_at = CURRENT_TIMESTAMP
                     WHERE run_id = %s
                       AND lease_owner = %s
                       AND fencing_token = %s
                    RETURNING run_id, task_id, owner_id, status, fencing_token,
                              lease_owner, lease_expires_at, attempt_count,
                              cancellation_requested_at
                    """,
                    (
                        status.value,
                        grant.run_id,
                        grant.worker_id,
                        grant.fencing_token,
                    ),
                )
                row = cursor.fetchone()
        if row is None:
            raise LostLeaseError("worker lease is no longer current")
        return self._run_from_row(row)

    def record_inbox(self, consumer_name: str, message_id: str) -> None:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO inbox_receipts (consumer_name, message_id)
                    VALUES (%s, %s)
                    ON CONFLICT (consumer_name, message_id) DO NOTHING
                    """,
                    (consumer_name, message_id),
                )

    def has_inbox(self, consumer_name: str, message_id: str) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS(
                    SELECT 1 FROM inbox_receipts
                     WHERE consumer_name = %s AND message_id = %s
                )
                """,
                (consumer_name, message_id),
            )
            return bool(cursor.fetchone()[0])

    def enqueue_recoveries(
        self,
        *,
        now: datetime,
        grace_period: timedelta,
        limit: int,
    ) -> tuple[OutboxMessage, ...]:
        created: list[OutboxMessage] = []
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT run_id
                      FROM production_run_control AS run
                     WHERE run.status IN ('RUNNING', 'STOPPING')
                       AND run.lease_expires_at IS NOT NULL
                       AND run.lease_expires_at + %s <= %s
                       AND NOT EXISTS (
                           SELECT 1
                             FROM outbox_messages AS message
                            WHERE message.aggregate_id = run.run_id
                              AND message.payload->>'reason' = 'RECOVER'
                              AND message.published_at IS NULL
                              AND message.quarantined_at IS NULL
                       )
                     ORDER BY run.lease_expires_at, run.run_id
                     FOR UPDATE SKIP LOCKED
                     LIMIT %s
                    """,
                    (grace_period, now, limit),
                )
                run_ids = [row[0] for row in cursor.fetchall()]
                for run_id in run_ids:
                    message_id = f"outbox-{uuid.uuid4().hex}"
                    payload = {
                        "message_id": message_id,
                        "payload_version": 1,
                        "run_id": run_id,
                        "reason": "RECOVER",
                    }
                    cursor.execute(
                        """
                        INSERT INTO outbox_messages (
                            message_id, aggregate_type, aggregate_id,
                            aggregate_version, topic, payload_version,
                            payload, available_at
                        )
                        SELECT %s, 'RUN', %s,
                               COALESCE(MAX(aggregate_version), 0) + 1,
                               'agent.run', 1, %s::jsonb, %s
                          FROM outbox_messages
                         WHERE aggregate_id = %s
                        RETURNING message_id, aggregate_id, aggregate_version,
                                  topic, payload_version, payload, available_at,
                                  attempts, published_at, lease_owner,
                                  lease_expires_at, quarantined_at
                        """,
                        (
                            message_id,
                            run_id,
                            json.dumps(payload, separators=(",", ":")),
                            now,
                            run_id,
                        ),
                    )
                    created.append(self._outbox_from_row(cursor.fetchone()))
        return tuple(created)

    _OUTBOX_SELECT = """
        SELECT message_id, aggregate_id, aggregate_version, topic,
               payload_version, payload, available_at, attempts,
               published_at, lease_owner, lease_expires_at, quarantined_at
          FROM outbox_messages
    """

    _RUN_SELECT = """
        SELECT run_id, task_id, owner_id, status, fencing_token,
               lease_owner, lease_expires_at, attempt_count,
               cancellation_requested_at
          FROM production_run_control
    """

    @staticmethod
    def _outbox_from_row(row) -> OutboxMessage:
        payload = json.loads(row[5]) if isinstance(row[5], str) else row[5]
        command = RunCommand(
            message_id=payload["message_id"],
            payload_version=payload["payload_version"],
            run_id=payload["run_id"],
            reason=payload["reason"],
        )
        return OutboxMessage(
            message_id=row[0],
            aggregate_id=row[1],
            aggregate_version=row[2],
            topic=row[3],
            payload_version=row[4],
            command=command,
            available_at=row[6],
            attempts=row[7],
            published_at=row[8],
            lease_owner=row[9],
            lease_expires_at=row[10],
            quarantined_at=row[11],
        )

    @staticmethod
    def _run_from_row(row) -> ProductionRun:
        return ProductionRun(
            run_id=row[0],
            task_id=row[1],
            owner_id=row[2],
            status=ProductionRunStatus(row[3]),
            fencing_token=row[4],
            lease_owner=row[5],
            lease_expires_at=row[6],
            attempt_count=row[7],
            cancellation_requested_at=row[8],
        )
