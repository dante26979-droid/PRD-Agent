"""Durable model-call attempt semantics used by Agent Workers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock
from typing import Any, Mapping, Protocol
import json
import uuid


class ModelAttemptStatus(StrEnum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"


@dataclass(frozen=True)
class ModelAttempt:
    attempt_id: str
    tenant_id: str
    owner_id: str
    run_id: str
    attempt_key: str
    operation: str
    prompt_version: str
    provider: str
    request_hash: str
    status: ModelAttemptStatus
    response_metadata: Mapping[str, Any]
    token_usage: Mapping[str, Any]
    error_category: str | None
    started_at: datetime
    completed_at: datetime | None = None


class ModelAttemptStore(Protocol):
    def begin(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        run_id: str,
        attempt_key: str,
        operation: str,
        prompt_version: str,
        provider: str,
        request_hash: str,
        now: datetime | None = None,
    ) -> ModelAttempt: ...

    def finish(
        self,
        attempt_id: str,
        *,
        status: ModelAttemptStatus,
        response_metadata: Mapping[str, Any] | None = None,
        token_usage: Mapping[str, Any] | None = None,
        error_category: str | None = None,
        now: datetime | None = None,
    ) -> ModelAttempt: ...


class AttemptConflict(ValueError):
    pass


class InMemoryModelAttemptStore:
    """Reference implementation with stable attempt-key de-duplication."""

    def __init__(self) -> None:
        self._attempts: dict[str, ModelAttempt] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self._lock = RLock()

    def begin(self, **kwargs) -> ModelAttempt:
        now = kwargs.pop("now", None) or datetime.now(timezone.utc)
        key = (kwargs["run_id"], kwargs["attempt_key"])
        with self._lock:
            existing_id = self._by_key.get(key)
            if existing_id is not None:
                existing = self._attempts[existing_id]
                if existing.request_hash != kwargs["request_hash"]:
                    raise AttemptConflict(
                        "attempt key was reused with a different request"
                    )
                return deepcopy(existing)
            attempt = ModelAttempt(
                attempt_id=f"attempt-{uuid.uuid4().hex}",
                status=ModelAttemptStatus.STARTED,
                response_metadata={},
                token_usage={},
                error_category=None,
                started_at=now,
                **kwargs,
            )
            self._attempts[attempt.attempt_id] = attempt
            self._by_key[key] = attempt.attempt_id
            return deepcopy(attempt)

    def finish(
        self,
        attempt_id: str,
        *,
        status: ModelAttemptStatus,
        response_metadata: Mapping[str, Any] | None = None,
        token_usage: Mapping[str, Any] | None = None,
        error_category: str | None = None,
        now: datetime | None = None,
    ) -> ModelAttempt:
        if status is ModelAttemptStatus.STARTED:
            raise ValueError("a started attempt cannot be finished as STARTED")
        with self._lock:
            current = self._attempts[attempt_id]
            if current.status is not ModelAttemptStatus.STARTED:
                return deepcopy(current)
            updated = replace(
                current,
                status=status,
                response_metadata=dict(response_metadata or {}),
                token_usage=dict(token_usage or {}),
                error_category=error_category,
                completed_at=now or datetime.now(timezone.utc),
            )
            self._attempts[attempt_id] = updated
            return deepcopy(updated)

    def get(self, attempt_id: str) -> ModelAttempt:
        with self._lock:
            return deepcopy(self._attempts[attempt_id])

    def get_by_key(self, run_id: str, attempt_key: str) -> ModelAttempt | None:
        with self._lock:
            attempt_id = self._by_key.get((run_id, attempt_key))
            return deepcopy(self._attempts[attempt_id]) if attempt_id else None


class PostgresModelAttemptStore:
    """Short-transaction PostgreSQL implementation for worker-side attempts."""

    def __init__(self, connection) -> None:
        self.connection = connection

    def begin(self, **kwargs) -> ModelAttempt:
        now = kwargs.pop("now", None) or datetime.now(timezone.utc)
        attempt_id = f"attempt-{uuid.uuid4().hex}"
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO model_attempts (
                        attempt_id, tenant_id, owner_id, run_id, attempt_key,
                        operation, prompt_version, provider, request_hash,
                        status, started_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, 'STARTED', %s
                    )
                    ON CONFLICT (run_id, attempt_key) DO NOTHING
                    RETURNING attempt_id, tenant_id, owner_id, run_id,
                              attempt_key, operation, prompt_version, provider,
                              request_hash, status, response_metadata,
                              token_usage, error_category, started_at,
                              completed_at
                    """,
                    (
                        attempt_id,
                        kwargs["tenant_id"],
                        kwargs["owner_id"],
                        kwargs["run_id"],
                        kwargs["attempt_key"],
                        kwargs["operation"],
                        kwargs["prompt_version"],
                        kwargs["provider"],
                        kwargs["request_hash"],
                        now,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        SELECT attempt_id, tenant_id, owner_id, run_id,
                               attempt_key, operation, prompt_version, provider,
                               request_hash, status, response_metadata,
                               token_usage, error_category, started_at,
                               completed_at
                          FROM model_attempts
                         WHERE run_id = %s AND attempt_key = %s
                        """,
                        (kwargs["run_id"], kwargs["attempt_key"]),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("model attempt disappeared")
                    if row[8] != kwargs["request_hash"]:
                        raise AttemptConflict(
                            "attempt key was reused with a different request"
                        )
        return self._from_row(row)

    def finish(
        self,
        attempt_id: str,
        *,
        status: ModelAttemptStatus,
        response_metadata: Mapping[str, Any] | None = None,
        token_usage: Mapping[str, Any] | None = None,
        error_category: str | None = None,
        now: datetime | None = None,
    ) -> ModelAttempt:
        if status is ModelAttemptStatus.STARTED:
            raise ValueError("a started attempt cannot be finished as STARTED")
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE model_attempts
                       SET status = %s,
                           response_metadata = %s::jsonb,
                           token_usage = %s::jsonb,
                           error_category = %s,
                           completed_at = %s
                     WHERE attempt_id = %s AND status = 'STARTED'
                    RETURNING attempt_id, tenant_id, owner_id, run_id,
                              attempt_key, operation, prompt_version, provider,
                              request_hash, status, response_metadata,
                              token_usage, error_category, started_at,
                              completed_at
                    """,
                    (
                        status.value,
                        json.dumps(response_metadata or {}, separators=(",", ":")),
                        json.dumps(token_usage or {}, separators=(",", ":")),
                        error_category,
                        now or datetime.now(timezone.utc),
                        attempt_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """
                        SELECT attempt_id, tenant_id, owner_id, run_id,
                               attempt_key, operation, prompt_version, provider,
                               request_hash, status, response_metadata,
                               token_usage, error_category, started_at,
                               completed_at
                          FROM model_attempts WHERE attempt_id = %s
                        """,
                        (attempt_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise KeyError(attempt_id)
        return self._from_row(row)

    @staticmethod
    def _from_row(row) -> ModelAttempt:
        def mapping(value):
            if isinstance(value, str):
                return json.loads(value)
            return value or {}

        return ModelAttempt(
            attempt_id=row[0],
            tenant_id=row[1],
            owner_id=row[2],
            run_id=row[3],
            attempt_key=row[4],
            operation=row[5],
            prompt_version=row[6],
            provider=row[7],
            request_hash=row[8],
            status=ModelAttemptStatus(row[9]),
            response_metadata=mapping(row[10]),
            token_usage=mapping(row[11]),
            error_category=row[12],
            started_at=row[13],
            completed_at=row[14],
        )
