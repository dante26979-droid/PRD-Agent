"""PostgreSQL implementation of the production dispatch control contract."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import uuid

from prd_agent.hashing import sha256_json
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
        max_global_runnable: int = 30,
        max_runnable_per_owner: int = 1,
    ) -> OutboxMessage:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.queue_slots')")
                queue_enabled = cursor.fetchone()[0] is not None
                status = "QUEUED"
                admitted = True
                if queue_enabled:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        ("prd-agent-run-admission",),
                    )
                    cursor.execute(
                        """
                        SELECT COUNT(*)
                          FROM production_run_control
                         WHERE queue_slot_acquired = TRUE
                           AND status IN ('QUEUED', 'RUNNING')
                        """
                    )
                    global_runnable = cursor.fetchone()[0]
                    cursor.execute(
                        """
                        SELECT COUNT(*)
                          FROM production_run_control
                         WHERE tenant_id = %s AND owner_id = %s
                           AND queue_slot_acquired = TRUE
                           AND status IN ('QUEUED', 'RUNNING')
                        """,
                        (tenant_id, owner_id),
                    )
                    owner_runnable = cursor.fetchone()[0]
                    admitted = (
                        global_runnable < max_global_runnable
                        and owner_runnable < max_runnable_per_owner
                    )
                    status = "QUEUED" if admitted else "WAITING_CAPACITY"
                if queue_enabled:
                    cursor.execute(
                        """
                        INSERT INTO production_run_control (
                            run_id, task_id, tenant_id, owner_id, status,
                            queue_slot_acquired, queue_admitted_at,
                            created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (run_id) DO NOTHING
                        RETURNING run_id
                        """,
                        (
                            run_id,
                            task_id,
                            tenant_id,
                            owner_id,
                            status,
                            admitted,
                            now if admitted else None,
                            now,
                            now,
                        ),
                    )
                else:
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
                    if queue_enabled and admitted:
                        cursor.execute(
                            """
                            INSERT INTO queue_slots (
                                slot_id, run_id, tenant_id, owner_id,
                                state, acquired_at
                            ) VALUES (%s, %s, %s, %s, 'ACQUIRED', %s)
                            """,
                            (
                                f"slot-{uuid.uuid4().hex}",
                                run_id,
                                tenant_id,
                                owner_id,
                                now,
                            ),
                        )
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
                            now
                            if admitted
                            else now + timedelta(days=36500),
                        ),
                    )
                cursor.execute(self._OUTBOX_SELECT + " WHERE aggregate_id = %s", (run_id,))
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("run exists without its dispatch outbox")
        return self._outbox_from_row(row)

    def admit_waiting_runs(
        self,
        *,
        now: datetime,
        max_global_runnable: int = 30,
        max_runnable_per_owner: int = 1,
        limit: int = 20,
    ) -> tuple[ProductionRun, ...]:
        """Promote capacity waiters using PostgreSQL as the authority."""

        promoted: list[ProductionRun] = []
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("prd-agent-run-admission",),
                )
                while len(promoted) < limit:
                    cursor.execute(
                        """
                        SELECT COUNT(*)
                          FROM production_run_control
                         WHERE queue_slot_acquired = TRUE
                           AND status IN ('QUEUED', 'RUNNING')
                        """
                    )
                    if cursor.fetchone()[0] >= max_global_runnable:
                        break
                    cursor.execute(
                        """
                        SELECT run.run_id, run.tenant_id, run.owner_id
                          FROM production_run_control AS run
                         WHERE run.status = 'WAITING_CAPACITY'
                           AND run.queue_slot_acquired = FALSE
                           AND (
                               SELECT COUNT(*)
                                 FROM production_run_control AS active
                                WHERE active.tenant_id = run.tenant_id
                                  AND active.owner_id = run.owner_id
                                  AND active.queue_slot_acquired = TRUE
                                  AND active.status IN ('QUEUED', 'RUNNING')
                           ) < %s
                         ORDER BY COALESCE(
                                      (
                                          SELECT last_admitted_at
                                            FROM scheduler_cursors AS cursor
                                           WHERE cursor.tenant_id = run.tenant_id
                                             AND cursor.owner_id = run.owner_id
                                      ), TIMESTAMPTZ 'epoch'
                                  ), run.created_at, run.run_id
                         FOR UPDATE OF run SKIP LOCKED
                         LIMIT 1
                        """,
                        (max_runnable_per_owner,),
                    )
                    candidate = cursor.fetchone()
                    if candidate is None:
                        break
                    run_id, tenant_id, owner_id = candidate
                    cursor.execute(
                        """
                        UPDATE production_run_control
                           SET status = 'QUEUED',
                               queue_slot_acquired = TRUE,
                               queue_admitted_at = %s,
                               updated_at = %s
                         WHERE run_id = %s
                        """,
                        (now, now, run_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO queue_slots (
                            slot_id, run_id, tenant_id, owner_id,
                            state, acquired_at
                        ) VALUES (%s, %s, %s, %s, 'ACQUIRED', %s)
                        ON CONFLICT (run_id) DO UPDATE
                            SET state = 'ACQUIRED', released_at = NULL,
                                acquired_at = EXCLUDED.acquired_at
                        """,
                        (
                            f"slot-{uuid.uuid4().hex}",
                            run_id,
                            tenant_id,
                            owner_id,
                            now,
                        ),
                    )
                    cursor.execute(
                        """
                        UPDATE outbox_messages
                           SET available_at = %s
                         WHERE aggregate_id = %s
                           AND published_at IS NULL
                           AND quarantined_at IS NULL
                        """,
                        (now, run_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO scheduler_cursors (
                            tenant_id, owner_id, last_admitted_at,
                            last_run_id, updated_at
                        ) VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (tenant_id, owner_id) DO UPDATE SET
                            last_admitted_at = EXCLUDED.last_admitted_at,
                            last_run_id = EXCLUDED.last_run_id,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (tenant_id, owner_id, now, run_id, now),
                    )
                    cursor.execute(self._RUN_SELECT + " WHERE run_id = %s", (run_id,))
                    promoted.append(self._run_from_row(cursor.fetchone()))
        return tuple(promoted)

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
                               payload->>'reason' = 'RECOVER'
                               OR EXISTS (
                                   SELECT 1
                                     FROM production_run_control AS run
                                    WHERE run.run_id = outbox_messages.aggregate_id
                                      AND run.status = 'QUEUED'
                               )
                           )
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
        idempotency_key: str,
        request_hash: str,
    ) -> ProductionRun:
        new_run_id = f"run-{uuid.uuid4().hex}"
        key_hash = sha256_json({"idempotency_key": idempotency_key})
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO command_idempotency (
                        tenant_id, owner_id, operation,
                        idempotency_key_hash, request_hash,
                        resource_type, resource_id, created_at
                    ) VALUES (
                        'local', %s, 'RETRY_RUN', %s, %s,
                        'RUN', %s, %s
                    )
                    ON CONFLICT (
                        tenant_id, owner_id, operation,
                        idempotency_key_hash
                    ) DO NOTHING
                    RETURNING resource_id
                    """,
                    (
                        owner_id,
                        key_hash,
                        request_hash,
                        new_run_id,
                        now,
                    ),
                )
                reserved = cursor.fetchone()
                if reserved is None:
                    cursor.execute(
                        """
                        SELECT request_hash, resource_id
                          FROM command_idempotency
                         WHERE tenant_id = 'local'
                           AND owner_id = %s
                           AND operation = 'RETRY_RUN'
                           AND idempotency_key_hash = %s
                        """,
                        (owner_id, key_hash),
                    )
                    replay = cursor.fetchone()
                    if replay is None or replay[0] != request_hash:
                        raise ValueError(
                            "retry idempotency key was used with "
                            "different input"
                        )
                    new_run_id = replay[1]
                else:
                    cursor.execute(
                        self._RUN_SELECT
                        + " WHERE run_id = %s AND owner_id = %s FOR UPDATE",
                        (run_id, owner_id),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise KeyError(run_id)
                    previous = self._run_from_row(row)
                    if previous.status not in {
                        ProductionRunStatus.FAILED,
                        ProductionRunStatus.STOPPED,
                    }:
                        raise ValueError(
                            "only failed or stopped runs can be retried"
                        )
                    cursor.execute(
                        """
                        INSERT INTO production_run_control (
                            run_id, task_id, tenant_id, owner_id, status,
                            created_at, updated_at
                        ) VALUES (
                            %s, %s, 'local', %s, 'QUEUED', %s, %s
                        )
                        """,
                        (
                            new_run_id,
                            previous.task_id,
                            owner_id,
                            now,
                            now,
                        ),
                    )
                    message_id = f"outbox-{uuid.uuid4().hex}"
                    payload = {
                        "message_id": message_id,
                        "payload_version": 1,
                        "run_id": new_run_id,
                        "reason": "RETRY",
                    }
                    cursor.execute(
                        """
                        INSERT INTO outbox_messages (
                            message_id, aggregate_type, aggregate_id,
                            aggregate_version, topic, payload_version,
                            payload, available_at
                        ) VALUES (
                            %s, 'RUN', %s, 1, 'agent.run', 1,
                            %s::jsonb, %s
                        )
                        """,
                        (
                            message_id,
                            new_run_id,
                            json.dumps(payload, separators=(",", ":")),
                            now,
                        ),
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
                if row is not None:
                    cursor.execute("SELECT to_regclass('public.queue_slots')")
                    if cursor.fetchone()[0] is not None:
                        cursor.execute(
                            """
                            UPDATE production_run_control
                               SET queue_slot_acquired = FALSE
                             WHERE run_id = %s
                            """,
                            (grant.run_id,),
                        )
                        cursor.execute(
                            """
                            UPDATE queue_slots
                               SET state = 'RELEASED', released_at = CURRENT_TIMESTAMP
                             WHERE run_id = %s AND state = 'ACQUIRED'
                            """,
                            (grant.run_id,),
                        )
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
                              AND (
                                  message.published_at IS NULL
                                  OR message.quarantined_at IS NOT NULL
                              )
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
