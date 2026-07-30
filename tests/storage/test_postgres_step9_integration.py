from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import time
import uuid

import pytest

from prd_agent.export.models import (
    CreateIdempotencyCapability,
    ExportIntent,
    ExportMode,
    ExportRun,
    ExportRunStatus,
    ExportSourceSnapshot,
    ExternalDocumentBinding,
    ProviderDocumentResult,
)
from prd_agent.export.service import ExportApplicationService
from prd_agent.hashing import sha256_json
from prd_agent.storage.postgres_export import PostgresExportStore
from prd_agent.storage.postgres import PostgresWorkflowRepository


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
    competing_store = PostgresExportStore.from_dsn(
        os.environ["PRD_AGENT_TEST_DATABASE_DSN"],
        ReversibleProtector(),
    )
    try:
        migration = Path(
            "infra/local/migrations/20260727_step9_external_integrations.sql"
        ).read_text(encoding="utf-8")
        remediation = Path(
            "infra/local/migrations/"
            "20260728_server_deployment_remediation.sql"
        ).read_text(encoding="utf-8")
        with store.connection.cursor() as cursor:
            cursor.execute(migration)
            cursor.execute(remediation)
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
        assert store.claim_intent(
            intent_id,
            owner_id,
            run_id,
            now=now,
            claim_expires_at=now + timedelta(minutes=1),
        )
        assert not competing_store.claim_intent(
            intent_id,
            owner_id,
            f"competing-{run_id}",
            now=now,
            claim_expires_at=now + timedelta(minutes=1),
        )
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
        competing_store.connection.close()
        store.connection.close()


def test_export_confirmation_survives_postgres_session_timezone_roundtrip():
    suffix = uuid.uuid4().hex
    task_id = f"task-confirmation-{suffix}"
    owner_id = "owner-confirmation"
    now = datetime(2026, 7, 27, 4, 0, 0, 123456, tzinfo=timezone.utc)
    store = PostgresExportStore.from_dsn(
        os.environ["PRD_AGENT_TEST_DATABASE_DSN"],
        ReversibleProtector(),
    )

    class Source:
        def get_export_source(self, requested_task_id, requested_owner_id):
            assert (requested_task_id, requested_owner_id) == (task_id, owner_id)
            return ExportSourceSnapshot(
                task_id=task_id,
                owner_id=owner_id,
                task_status="COMPLETED",
                task_version=7,
                document_id=f"document-{suffix}",
                document_version=3,
                markdown="# Timezone-safe confirmation\n\nExport body.",
            )

    class Gateway:
        create_idempotency_capability = (
            CreateIdempotencyCapability.PROVIDER_KEY
        )

        def create_document(self, document, *, idempotency_key):
            return ProviderDocumentResult(
                external_id=f"feishu-{suffix}",
                safe_url="https://example.feishu.cn/wiki/safe",
                title=document.title,
                provider_revision="revision-1",
            )

    try:
        migration = Path(
            "infra/local/migrations/20260727_step9_external_integrations.sql"
        ).read_text(encoding="utf-8")
        with store.connection.cursor() as cursor:
            cursor.execute(migration)
            cursor.execute("SET TIME ZONE 'Asia/Shanghai'")
            cursor.execute(
                """
                INSERT INTO prd_tasks (
                    task_id, owner_id, title, status, version,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, 'COMPLETED', 7, %s, %s)
                """,
                (task_id, owner_id, "Confirmation", now, now),
            )
        store.connection.commit()
        service = ExportApplicationService(
            Source(),
            store,
            Gateway(),
            confirmation_secret=b"test-confirmation-secret",
            now=lambda: now,
        )

        preview = service.preview(
            task_id=task_id,
            owner_id=owner_id,
            mode=ExportMode.CREATE,
            expected_task_version=7,
            idempotency_key=f"preview-{suffix}",
        )
        result = service.execute(
            intent_id=preview.intent_id,
            confirmation_token=preview.confirmation_token,
            owner_id=owner_id,
            expected_task_version=7,
            idempotency_key=f"execute-{suffix}",
        )

        assert result.status == ExportRunStatus.SUCCEEDED
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


def test_export_does_not_hold_event_transaction_open_during_provider_call():
    suffix = uuid.uuid4().hex
    task_id = f"task-provider-wait-{suffix}"
    owner_id = "owner-provider-wait"
    now = datetime.now(timezone.utc)
    store = PostgresExportStore.from_dsn(
        os.environ["PRD_AGENT_TEST_DATABASE_DSN"],
        ReversibleProtector(),
    )
    event_store = PostgresWorkflowRepository.from_dsn(
        os.environ["PRD_AGENT_TEST_DATABASE_DSN"]
    )

    class Source:
        def get_export_source(self, requested_task_id, requested_owner_id):
            assert (requested_task_id, requested_owner_id) == (task_id, owner_id)
            return ExportSourceSnapshot(
                task_id=task_id,
                owner_id=owner_id,
                task_status="COMPLETED",
                task_version=7,
                document_id=f"document-{suffix}",
                document_version=3,
                markdown="# Provider wait\n\nExport body.",
            )

    class SlowGateway:
        create_idempotency_capability = (
            CreateIdempotencyCapability.PROVIDER_KEY
        )

        def create_document(self, document, *, idempotency_key):
            time.sleep(0.1)
            return ProviderDocumentResult(
                external_id=f"feishu-{suffix}",
                safe_url="https://example.feishu.cn/wiki/safe",
                title=document.title,
                provider_revision="revision-1",
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
                (task_id, owner_id, "Provider wait", now, now),
            )
        store.connection.commit()
        with event_store.connection.cursor() as cursor:
            cursor.execute(
                "SET idle_in_transaction_session_timeout = '50ms'"
            )
        event_store.commit()
        service = ExportApplicationService(
            Source(),
            store,
            SlowGateway(),
            confirmation_secret=b"test-confirmation-secret",
            event_sink=event_store,
        )
        preview = service.preview(
            task_id=task_id,
            owner_id=owner_id,
            mode=ExportMode.CREATE,
            expected_task_version=7,
            idempotency_key=f"preview-{suffix}",
        )
        event_store.commit()

        result = service.execute(
            intent_id=preview.intent_id,
            confirmation_token=preview.confirmation_token,
            owner_id=owner_id,
            expected_task_version=7,
            idempotency_key=f"execute-{suffix}",
        )
        event_store.commit()

        assert result.status == ExportRunStatus.SUCCEEDED
    finally:
        with store.connection.cursor() as cursor:
            cursor.execute("DELETE FROM domain_events WHERE task_id = %s", (task_id,))
            cursor.execute("DELETE FROM export_runs WHERE task_id = %s", (task_id,))
            cursor.execute(
                "DELETE FROM external_document_bindings WHERE task_id = %s",
                (task_id,),
            )
            cursor.execute("DELETE FROM export_intents WHERE task_id = %s", (task_id,))
            cursor.execute("DELETE FROM prd_tasks WHERE task_id = %s", (task_id,))
        store.connection.commit()
        store.connection.close()
        event_store.connection.close()
