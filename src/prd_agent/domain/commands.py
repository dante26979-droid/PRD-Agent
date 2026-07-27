from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StartTask:
    message: str
    idempotency_key: str
    actor_id: str = "local-user"


@dataclass(frozen=True)
class ReplyToTask:
    task_id: str
    message: str
    expected_task_version: int
    idempotency_key: str
    actor_id: str = "local-user"


@dataclass(frozen=True)
class ConfirmOutline:
    task_id: str
    outline_version: int
    expected_task_version: int
    idempotency_key: str
    actor_id: str = "local-user"


@dataclass(frozen=True)
class ConfirmUnit:
    task_id: str
    unit_id: str
    expected_task_version: int
    idempotency_key: str
    actor_id: str = "local-user"


@dataclass(frozen=True)
class FinalizePrd:
    task_id: str
    document_id: str
    content_hash: str
    expected_task_version: int
    idempotency_key: str
    actor_id: str = "local-user"


@dataclass(frozen=True)
class ApproveRevisionPlan:
    task_id: str
    document_id: str
    issue_ids: tuple[str, ...]
    unit_ids: tuple[str, ...]
    expected_task_version: int
    idempotency_key: str
    actor_id: str = "local-user"


@dataclass(frozen=True)
class ReopenPrd:
    task_id: str
    unit_ids: tuple[str, ...]
    reason: str
    expected_task_version: int
    idempotency_key: str
    actor_id: str = "local-user"


@dataclass(frozen=True)
class StopRun:
    task_id: str
    run_id: str
    expected_task_version: int
    idempotency_key: str
    actor_id: str = "local-user"


@dataclass(frozen=True)
class RetryRun:
    task_id: str
    run_id: str
    expected_task_version: int
    idempotency_key: str
    actor_id: str = "local-user"
