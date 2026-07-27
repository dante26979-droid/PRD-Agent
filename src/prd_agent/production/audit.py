from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Protocol, TypeVar


class AuditUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuditEvent:
    audit_event_id: str
    tenant_id: str
    principal_hash: str
    action: str
    target_type: str
    target_id: str
    correlation_id: str
    result: str = "STARTED"
    occurred_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class AuditStore(Protocol):
    def append(self, event: AuditEvent) -> None: ...


class PostgresAuditStore:
    def __init__(self, connection) -> None:
        self.connection = connection

    def append(self, event: AuditEvent) -> None:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO audit_events (
                        audit_event_id, tenant_id, principal_hash, action,
                        target_type, target_id, result, correlation_id,
                        occurred_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (audit_event_id) DO UPDATE
                        SET result = EXCLUDED.result,
                            occurred_at = EXCLUDED.occurred_at
                    """,
                    (
                        event.audit_event_id,
                        event.tenant_id,
                        event.principal_hash,
                        event.action,
                        event.target_type,
                        event.target_id,
                        event.result,
                        event.correlation_id,
                        event.occurred_at,
                    ),
                )


T = TypeVar("T")


def perform_audited_write(
    *,
    audit_store: AuditStore,
    intent: AuditEvent,
    action: Callable[[], T],
) -> T:
    try:
        audit_store.append(intent)
    except Exception as exc:
        raise AuditUnavailableError("audit write failed") from exc
    try:
        result = action()
    except Exception:
        try:
            audit_store.append(
                replace(
                    intent,
                    result="FAILED",
                    occurred_at=datetime.now(timezone.utc),
                )
            )
        finally:
            raise
    try:
        audit_store.append(
            replace(
                intent,
                result="SUCCEEDED",
                occurred_at=datetime.now(timezone.utc),
            )
        )
    except Exception as exc:
        raise AuditUnavailableError("audit completion write failed") from exc
    return result
