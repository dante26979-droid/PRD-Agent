from __future__ import annotations

from prd_agent.domain.entities import IdempotencyRecord
from prd_agent.domain.errors import IdempotencyConflict


def validate_replay(record: IdempotencyRecord, input_hash: str) -> str:
    if record.input_hash != input_hash:
        raise IdempotencyConflict("idempotency key was already used with different input")
    return record.task_id

