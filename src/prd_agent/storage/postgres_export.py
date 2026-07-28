from __future__ import annotations

from datetime import datetime

from prd_agent.export.models import (
    ExportIntent,
    ExportRun,
    ExternalDocumentBinding,
)
from prd_agent.hashing import sha256_json
from prd_agent.integrations.errors import IntegrationError, IntegrationErrorCode


class PostgresExportStore:
    def __init__(self, connection, external_id_protector) -> None:
        self.connection = connection
        self.external_id_protector = external_id_protector

    @classmethod
    def from_dsn(cls, dsn: str, external_id_protector):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError(
                "PostgreSQL export support requires: "
                "python -m pip install '.[postgres]'"
            ) from exc
        return cls(psycopg.connect(dsn), external_id_protector)

    def save_intent(self, intent: ExportIntent) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO export_intents (
                    intent_id, owner_id, task_id, mode, task_version,
                    document_id, document_version, content_hash,
                    preview_title, unresolved_items_json, bound_title,
                    bound_safe_url, expires_at, consumed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (intent_id) DO NOTHING
                """,
                (
                    intent.intent_id,
                    intent.owner_id,
                    intent.task_id,
                    intent.mode.value,
                    intent.task_version,
                    intent.document_id,
                    intent.document_version,
                    intent.content_hash,
                    intent.title,
                    list(intent.unresolved_items),
                    intent.bound_title,
                    intent.bound_safe_url,
                    intent.expires_at,
                    intent.consumed_at,
                ),
            )
        self.connection.commit()

    def get_intent(self, intent_id: str, owner_id: str) -> ExportIntent:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT intent_id, task_id, owner_id, mode, task_version,
                       document_id, document_version, content_hash,
                       preview_title, unresolved_items_json, bound_title,
                       bound_safe_url, expires_at, consumed_at
                  FROM export_intents
                 WHERE intent_id = %s AND owner_id = %s
                """,
                (intent_id, owner_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise IntegrationError(
                IntegrationErrorCode.RESOURCE_NOT_FOUND,
                "export intent was not found",
            )
        return ExportIntent(
            intent_id=row[0],
            task_id=row[1],
            owner_id=row[2],
            mode=row[3],
            task_version=row[4],
            document_id=row[5],
            document_version=row[6],
            content_hash=row[7],
            title=row[8],
            unresolved_items=tuple(row[9]),
            bound_title=row[10],
            bound_safe_url=row[11],
            expires_at=row[12],
            consumed_at=row[13],
        )

    def get_preview_replay(self, owner_id: str, idempotency_key: str):
        key_hash = sha256_json({"idempotency_key": idempotency_key})
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT intent_id, idempotency_input_hash
                  FROM export_intents
                 WHERE owner_id = %s AND idempotency_key_hash = %s
                """,
                (owner_id, key_hash),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return row[1], self.get_intent(row[0], owner_id)

    def save_preview_replay(
        self,
        owner_id: str,
        idempotency_key: str,
        input_hash: str,
        intent: ExportIntent,
    ) -> None:
        key_hash = sha256_json({"idempotency_key": idempotency_key})
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE export_intents
                   SET idempotency_key_hash = %s,
                       idempotency_input_hash = %s
                 WHERE intent_id = %s AND owner_id = %s
                   AND (
                       idempotency_key_hash IS NULL
                       OR idempotency_key_hash = %s
                   )
                """,
                (
                    key_hash,
                    input_hash,
                    intent.intent_id,
                    owner_id,
                    key_hash,
                ),
            )
            if cursor.rowcount != 1:
                raise IntegrationError(
                    IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                    "idempotency key was used with different preview input",
                )
        self.connection.commit()

    def begin_preview(
        self,
        owner_id: str,
        idempotency_key: str,
        input_hash: str,
        intent: ExportIntent,
    ) -> ExportIntent | None:
        key_hash = sha256_json({"idempotency_key": idempotency_key})
        if intent.owner_id != owner_id:
            raise IntegrationError(
                IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                "preview owner does not match the idempotency reservation",
            )
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO export_intents (
                    intent_id, owner_id, task_id, mode, task_version,
                    document_id, document_version, content_hash,
                    preview_title, unresolved_items_json, bound_title,
                    bound_safe_url, idempotency_key_hash,
                    idempotency_input_hash, expires_at, consumed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (owner_id, idempotency_key_hash) DO NOTHING
                """,
                (
                    intent.intent_id,
                    intent.owner_id,
                    intent.task_id,
                    intent.mode.value,
                    intent.task_version,
                    intent.document_id,
                    intent.document_version,
                    intent.content_hash,
                    intent.title,
                    list(intent.unresolved_items),
                    intent.bound_title,
                    intent.bound_safe_url,
                    key_hash,
                    input_hash,
                    intent.expires_at,
                    intent.consumed_at,
                ),
            )
            inserted = cursor.rowcount == 1
        self.connection.commit()
        if inserted:
            return None
        replay = self.get_preview_replay(owner_id, idempotency_key)
        if replay is None:
            raise IntegrationError(
                IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                "preview idempotency reservation could not be restored",
            )
        replay_hash, existing = replay
        if replay_hash != input_hash:
            raise IntegrationError(
                IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                "idempotency key was used with different preview input",
            )
        return existing

    def save_binding(self, binding: ExternalDocumentBinding) -> None:
        protected = self.external_id_protector.protect(binding.external_id)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO external_document_bindings (
                    binding_id, owner_id, task_id, provider,
                    external_id_ciphertext, safe_url, display_title,
                    last_export_hash, last_document_version, provider_revision
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (owner_id, task_id, provider) DO UPDATE SET
                    external_id_ciphertext = EXCLUDED.external_id_ciphertext,
                    safe_url = EXCLUDED.safe_url,
                    display_title = EXCLUDED.display_title,
                    last_export_hash = EXCLUDED.last_export_hash,
                    last_document_version = EXCLUDED.last_document_version,
                    provider_revision = EXCLUDED.provider_revision,
                    updated_at = CURRENT_TIMESTAMP
                WHERE external_document_bindings.binding_id = EXCLUDED.binding_id
                """,
                (
                    binding.binding_id,
                    binding.owner_id,
                    binding.task_id,
                    binding.provider,
                    protected,
                    binding.safe_url,
                    binding.display_title,
                    binding.last_export_hash,
                    binding.last_document_version,
                    binding.provider_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise IntegrationError(
                    IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                    "task already has a different document binding",
                )
        self.connection.commit()

    def get_binding(
        self,
        task_id: str,
        owner_id: str,
    ) -> ExternalDocumentBinding | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT binding_id, task_id, owner_id, provider,
                       external_id_ciphertext, safe_url, display_title,
                       last_export_hash, last_document_version, provider_revision
                  FROM external_document_bindings
                 WHERE task_id = %s AND owner_id = %s AND provider = 'FEISHU'
                """,
                (task_id, owner_id),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return ExternalDocumentBinding(
            binding_id=row[0],
            task_id=row[1],
            owner_id=row[2],
            provider=row[3],
            external_id=self.external_id_protector.unprotect(row[4]),
            safe_url=row[5],
            display_title=row[6],
            last_export_hash=row[7],
            last_document_version=row[8],
            provider_revision=row[9],
        )

    def save_run(self, run: ExportRun) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                self._run_upsert_sql(),
                self._run_parameters(run, input_hash=None),
            )
        self.connection.commit()

    def begin_run(
        self,
        owner_id: str,
        idempotency_key: str,
        input_hash: str,
        run: ExportRun,
    ) -> ExportRun | None:
        expected_key_hash = sha256_json({"idempotency_key": idempotency_key})
        if run.owner_id != owner_id or run.idempotency_key_hash != expected_key_hash:
            raise IntegrationError(
                IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                "idempotency key does not match export run",
            )
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO export_runs (
                    export_run_id, intent_id, owner_id, task_id, mode,
                    task_version, document_version, content_hash, status,
                    idempotency_key_hash, idempotency_input_hash,
                    attempt_count, binding_id, safe_url, display_title,
                    error_code, retryable, created_at, completed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (owner_id, idempotency_key_hash) DO NOTHING
                """,
                self._run_parameters(run, input_hash=input_hash),
            )
            inserted = cursor.rowcount == 1
        self.connection.commit()
        if inserted:
            return None
        replay = self.get_replay(owner_id, idempotency_key)
        if replay is None:
            raise IntegrationError(
                IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                "idempotency reservation could not be restored",
            )
        replay_hash, existing = replay
        if replay_hash != input_hash:
            raise IntegrationError(
                IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                "idempotency key was used with different input",
            )
        return existing

    def get_replay(
        self,
        owner_id: str,
        idempotency_key: str,
    ) -> tuple[str, ExportRun] | None:
        key_hash = sha256_json({"idempotency_key": idempotency_key})
        with self.connection.cursor() as cursor:
            cursor.execute(
                self._run_select_sql(
                    "WHERE owner_id = %s AND idempotency_key_hash = %s"
                ),
                (owner_id, key_hash),
            )
            row = cursor.fetchone()
        if row is None or row[-1] is None:
            return None
        return row[-1], self._run(row[:-1])

    def save_replay(
        self,
        owner_id: str,
        idempotency_key: str,
        input_hash: str,
        run: ExportRun,
    ) -> None:
        expected_key_hash = sha256_json({"idempotency_key": idempotency_key})
        if run.owner_id != owner_id or run.idempotency_key_hash != expected_key_hash:
            raise IntegrationError(
                IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                "idempotency key does not match export run",
            )
        with self.connection.cursor() as cursor:
            cursor.execute(
                self._run_upsert_sql(),
                self._run_parameters(run, input_hash=input_hash),
            )
        self.connection.commit()

    def consume_intent(self, intent: ExportIntent) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE export_intents
                   SET consumed_at = %s
                 WHERE intent_id = %s AND owner_id = %s
                """,
                (intent.consumed_at, intent.intent_id, intent.owner_id),
            )
        self.connection.commit()

    def claim_intent(
        self,
        intent_id: str,
        owner_id: str,
        claim_id: str,
        *,
        now: datetime,
        claim_expires_at: datetime,
    ) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE export_intents
                   SET claimed_by = %s,
                       claim_expires_at = %s
                 WHERE intent_id = %s
                   AND owner_id = %s
                   AND consumed_at IS NULL
                   AND (
                       claimed_by IS NULL
                       OR claimed_by = %s
                       OR claim_expires_at <= %s
                   )
                """,
                (
                    claim_id,
                    claim_expires_at,
                    intent_id,
                    owner_id,
                    claim_id,
                    now,
                ),
            )
            claimed = cursor.rowcount == 1
        self.connection.commit()
        return claimed

    def release_intent_claim(
        self,
        intent_id: str,
        owner_id: str,
        claim_id: str,
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE export_intents
                   SET claimed_by = NULL,
                       claim_expires_at = NULL
                 WHERE intent_id = %s
                   AND owner_id = %s
                   AND claimed_by = %s
                   AND consumed_at IS NULL
                """,
                (intent_id, owner_id, claim_id),
            )
        self.connection.commit()

    def consume_claimed_intent(
        self,
        intent_id: str,
        owner_id: str,
        claim_id: str,
        *,
        consumed_at: datetime,
    ) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE export_intents
                   SET consumed_at = %s,
                       claimed_by = NULL,
                       claim_expires_at = NULL
                 WHERE intent_id = %s
                   AND owner_id = %s
                   AND claimed_by = %s
                   AND consumed_at IS NULL
                """,
                (consumed_at, intent_id, owner_id, claim_id),
            )
            consumed = cursor.rowcount == 1
        self.connection.commit()
        return consumed

    def list_runs(self, task_id: str, owner_id: str) -> tuple[ExportRun, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                self._run_select_sql(
                    "WHERE task_id = %s AND owner_id = %s "
                    "ORDER BY created_at DESC, export_run_id DESC"
                ),
                (task_id, owner_id),
            )
            rows = cursor.fetchall()
        return tuple(self._run(row[:-1]) for row in rows)

    @staticmethod
    def _run_select_sql(where: str) -> str:
        return f"""
            SELECT export_run_id, intent_id, task_id, owner_id, mode,
                   task_version, document_version, content_hash, status,
                   idempotency_key_hash, attempt_count, binding_id,
                   safe_url, display_title, error_code, retryable,
                   created_at, completed_at, idempotency_input_hash
              FROM export_runs
              {where}
        """

    @staticmethod
    def _run_upsert_sql() -> str:
        return """
            INSERT INTO export_runs (
                export_run_id, intent_id, owner_id, task_id, mode,
                task_version, document_version, content_hash, status,
                idempotency_key_hash, idempotency_input_hash,
                attempt_count, binding_id, safe_url, display_title,
                error_code, retryable, created_at, completed_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (export_run_id) DO UPDATE SET
                status = EXCLUDED.status,
                idempotency_input_hash = COALESCE(
                    EXCLUDED.idempotency_input_hash,
                    export_runs.idempotency_input_hash
                ),
                attempt_count = EXCLUDED.attempt_count,
                binding_id = EXCLUDED.binding_id,
                safe_url = EXCLUDED.safe_url,
                display_title = EXCLUDED.display_title,
                error_code = EXCLUDED.error_code,
                retryable = EXCLUDED.retryable,
                completed_at = EXCLUDED.completed_at
        """

    @staticmethod
    def _run_parameters(run: ExportRun, *, input_hash: str | None):
        return (
            run.export_run_id,
            run.intent_id,
            run.owner_id,
            run.task_id,
            run.mode.value,
            run.task_version,
            run.document_version,
            run.content_hash,
            run.status.value,
            run.idempotency_key_hash,
            input_hash,
            run.attempt_count,
            run.binding_id,
            run.safe_url,
            run.display_title,
            run.error_code,
            run.retryable,
            run.created_at,
            run.completed_at,
        )

    @staticmethod
    def _run(row) -> ExportRun:
        return ExportRun(
            export_run_id=row[0],
            intent_id=row[1],
            task_id=row[2],
            owner_id=row[3],
            mode=row[4],
            task_version=row[5],
            document_version=row[6],
            content_hash=row[7],
            status=row[8],
            idempotency_key_hash=row[9],
            attempt_count=row[10],
            binding_id=row[11],
            safe_url=row[12],
            display_title=row[13],
            error_code=row[14],
            retryable=row[15],
            created_at=row[16],
            completed_at=row[17],
        )
