from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import uuid

import pytest

from prd_agent.export.models import (
    ExportIntent,
    ExportMode,
    ExportRun,
    ExportRunStatus,
    ExternalDocumentBinding,
)
from prd_agent.hashing import sha256_json
from prd_agent.storage.postgres_export import PostgresExportStore


pytestmark = pytest.mark.skipif(
    not os.environ.get("PRD_AGENT_TEST_DATABASE_DSN"),
    reason="PRD_AGENT_TEST_DATABASE_DSN is required for PostgreSQL integration tests",
)


class ReversibleProtector:
    def protect(self, value: str) -> str:
        return "protected:" + value[::-1]

    def unprotect(self, value: str) -> str:
        assert value.startswith("protected:")
        return value.removeprefix("protected:")[::-1]


def test_postgres_export_binding_and_idempotency_contract():
    suffix = uuid.uuid4().hex
    task_id = f"task-step9-{suffix}"
    intent_id = f"intent-step9-{suffix}"
    run_id = f"run-step9-{suffix}"
    owner_id = "owner-step9"
    now = datetime.now(timezone.utc)
    store = PostgresExportStore.from_dsn(
        os.environ["PRD_AGENT_TEST_DATABASE_DSN"],
        ReversibleProtector(),
    )
    try:
        migration = Path(
            "infra/local/migrations/20260727_step9_external_integrations.sql"
        ).read_text(encoding="utf-8")
        with store.connection.cursor() as cursor:
            cursor.execute(migration)
            cursor.execute(
                """
                INSERT INTO prd_tasks (
                    task_id, owner_id, title, status, version,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, 'COMPLETED', 7, %s, %s)
                """,
                (task_id, owner_id, "Step 9", now, now),
            )
        store.connection.commit()
        intent = ExportIntent(
            intent_id=intent_id,
            task_id=task_id,
            owner_id=owner_id,
            mode=ExportMode.CREATE,
            task_version=7,
            document_id=f"document-{suffix}",
            document_version=3,
            content_hash="sha256:" + "a" * 64,
            title="Step 9",
            expires_at=now + timedelta(minutes=10),
        )
        assert store.begin_preview(
            owner_id,
            "preview-key",
            "preview-input",
            intent,
        ) is None
        assert store.begin_preview(
            owner_id,
            "preview-key",
            "preview-input",
            intent.model_copy(update={"intent_id": f"other-{intent_id}"}),
        ) == intent
        binding = ExternalDocumentBinding(
            binding_id=f"binding-{suffix}",
            task_id=task_id,
            owner_id=owner_id,
            external_id="private-feishu-id",
            safe_url="https://example.feishu.cn/docx/safe",
            display_title="Step 9",
            last_export_hash=intent.content_hash,
            last_document_version=3,
        )
        store.save_binding(binding)
        key_hash = sha256_json({"idempotency_key": "execute-key"})
        run = ExportRun(
            export_run_id=run_id,
            intent_id=intent_id,
            task_id=task_id,
            owner_id=owner_id,
            mode=ExportMode.CREATE,
            task_version=7,
            document_version=3,
            content_hash=intent.content_hash,
            status=ExportRunStatus.RUNNING,
            idempotency_key_hash=key_hash,
        )

        assert store.begin_run(
            owner_id, "execute-key", "execute-input", run
        ) is None
        replay = store.begin_run(
            owner_id, "execute-key", "execute-input", run
        )

        assert replay.export_run_id == run_id
        assert store.get_binding(task_id, owner_id).external_id == "private-feishu-id"
        assert store.get_preview_replay(owner_id, "preview-key")[1] == intent
    finally:
        with store.connection.cursor() as cursor:
            cursor.execute("DELETE FROM export_runs WHERE task_id = %s", (task_id,))
            cursor.execute(
                "DELETE FROM external_document_bindings WHERE task_id = %s",
                (task_id,),
            )
            cursor.execute("DELETE FROM export_intents WHERE task_id = %s", (task_id,))
            cursor.execute("DELETE FROM prd_tasks WHERE task_id = %s", (task_id,))
        store.connection.commit()
        store.connection.close()
