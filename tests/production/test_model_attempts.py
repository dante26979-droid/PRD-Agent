from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from prd_agent.production.model_attempts import (
    AttemptConflict,
    InMemoryModelAttemptStore,
    ModelAttemptStatus,
)


def test_same_attempt_key_replays_without_creating_a_second_attempt():
    store = InMemoryModelAttemptStore()
    started_at = datetime(2026, 7, 28, tzinfo=timezone.utc)
    first = store.begin(
        tenant_id="tenant-a",
        owner_id="alice",
        run_id="run-1",
        attempt_key="outline:1",
        operation="extract_requirement_brief",
        prompt_version="extract_requirement_brief.v1",
        provider="deepseek",
        request_hash="sha256:request",
        now=started_at,
    )
    replay = store.begin(
        tenant_id="tenant-a",
        owner_id="alice",
        run_id="run-1",
        attempt_key="outline:1",
        operation="extract_requirement_brief",
        prompt_version="extract_requirement_brief.v1",
        provider="deepseek",
        request_hash="sha256:request",
        now=started_at,
    )

    assert replay == first
    assert store.get_by_key("run-1", "outline:1") == first


def test_attempt_key_cannot_be_reused_for_different_input():
    store = InMemoryModelAttemptStore()
    args = dict(
        tenant_id="tenant-a",
        owner_id="alice",
        run_id="run-1",
        attempt_key="outline:1",
        operation="extract_requirement_brief",
        prompt_version="extract_requirement_brief.v1",
        provider="deepseek",
        request_hash="sha256:first",
    )
    store.begin(**args)

    with pytest.raises(AttemptConflict):
        store.begin(**{**args, "request_hash": "sha256:second"})


def test_completed_attempt_is_immutable_on_repeated_completion():
    store = InMemoryModelAttemptStore()
    attempt = store.begin(
        tenant_id="tenant-a",
        owner_id="alice",
        run_id="run-1",
        attempt_key="outline:1",
        operation="extract_requirement_brief",
        prompt_version="extract_requirement_brief.v1",
        provider="deepseek",
        request_hash="sha256:request",
    )
    completed_at = datetime(2026, 7, 28, 1, tzinfo=timezone.utc)
    completed = store.finish(
        attempt.attempt_id,
        status=ModelAttemptStatus.SUCCEEDED,
        response_metadata={"provider_request_id": "req-1"},
        token_usage={"total": 42},
        now=completed_at,
    )
    replay = store.finish(
        attempt.attempt_id,
        status=ModelAttemptStatus.FAILED,
        error_category="late-error",
    )

    assert completed.status is ModelAttemptStatus.SUCCEEDED
    assert replay == completed
    assert replay.completed_at == completed_at


def test_production_migration_contains_attempt_and_capacity_contracts():
    sql = Path(
        "infra/local/migrations/20260728_production_queue_and_attempts.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS queue_slots" in sql
    assert "CREATE TABLE IF NOT EXISTS scheduler_cursors" in sql
    assert "CREATE TABLE IF NOT EXISTS model_attempts" in sql
    assert "UNIQUE (run_id, attempt_key)" in sql
    assert "WAITING_CAPACITY" in sql
