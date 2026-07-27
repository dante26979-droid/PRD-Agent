from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import uuid

import pytest

from prd_agent.production.dispatch import (
    LostLeaseError,
    ProductionRunStatus,
)
from prd_agent.production.postgres_dispatch import PostgresProductionControlStore


def test_step10_migration_has_atomic_dispatch_and_claim_indexes():
    sql = Path(
        "infra/local/migrations/20260727_step10_production_profile.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS production_run_control" in sql
    assert "CREATE TABLE IF NOT EXISTS outbox_messages" in sql
    assert "CREATE TABLE IF NOT EXISTS inbox_receipts" in sql
    assert "ix_outbox_publishable" in sql
    assert "ix_production_run_recovery" in sql
    assert "payload JSONB NOT NULL" in sql


@pytest.mark.skipif(
    not os.environ.get("PRD_AGENT_TEST_DATABASE_DSN"),
    reason="PRD_AGENT_TEST_DATABASE_DSN is required",
)
def test_postgres_dispatch_is_atomic_claimable_and_fenced():
    now = datetime.now(timezone.utc)
    suffix = uuid.uuid4().hex
    run_id = f"run-production-{suffix}"
    migration = Path(
        "infra/local/migrations/20260727_step10_production_profile.sql"
    ).read_text(encoding="utf-8")
    store = PostgresProductionControlStore.from_dsn(
        os.environ["PRD_AGENT_TEST_DATABASE_DSN"]
    )
    second = PostgresProductionControlStore.from_dsn(
        os.environ["PRD_AGENT_TEST_DATABASE_DSN"]
    )
    try:
        with store.connection.cursor() as cursor:
            cursor.execute(migration)
        store.connection.commit()
        outbox = store.create_run_dispatch(
            run_id=run_id,
            task_id=f"task-{suffix}",
            owner_id="alice",
            reason="START_OR_RESUME",
            now=now,
        )

        first_claim = store.claim_outbox(
            publisher_id="publisher-a",
            now=now,
            lease_ttl=timedelta(seconds=10),
            limit=10,
        )
        second_claim = second.claim_outbox(
            publisher_id="publisher-b",
            now=now,
            lease_ttl=timedelta(seconds=10),
            limit=10,
        )
        stale = store.acquire_run(
            run_id,
            worker_id="worker-a",
            now=now,
            lease_ttl=timedelta(seconds=10),
        )
        current = second.acquire_run(
            run_id,
            worker_id="worker-b",
            now=now + timedelta(seconds=11),
            lease_ttl=timedelta(seconds=10),
        )

        result = second.complete_run(current, ProductionRunStatus.SUCCEEDED)

        assert [item.message_id for item in first_claim] == [outbox.message_id]
        assert outbox.message_id not in {
            item.message_id for item in second_claim
        }
        assert current.fencing_token == stale.fencing_token + 1
        assert result.status == ProductionRunStatus.SUCCEEDED
        with pytest.raises(LostLeaseError):
            store.complete_run(stale, ProductionRunStatus.FAILED)
    finally:
        with store.connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM inbox_receipts WHERE message_id LIKE %s",
                (f"%{suffix}%",),
            )
            cursor.execute(
                "DELETE FROM outbox_messages WHERE aggregate_id = %s",
                (run_id,),
            )
            cursor.execute(
                "DELETE FROM production_run_control WHERE run_id = %s",
                (run_id,),
            )
        store.connection.commit()
        second.connection.close()
        store.connection.close()
