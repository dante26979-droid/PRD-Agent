from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from typing import Protocol
import uuid

from prd_agent.hashing import canonical_json, sha256_json
from prd_agent.integrations.errors import IntegrationError, IntegrationErrorCode

from .formatter import FeishuExportFormatter
from .gateway import DocumentExportGateway
from .models import (
    CreateIdempotencyCapability,
    ExportIntent,
    ExportMode,
    ExportPreview,
    ExportRun,
    ExportRunStatus,
    ExportSourceSnapshot,
    ExternalDocumentBinding,
)


class ExportDocumentSource(Protocol):
    def get_export_source(
        self,
        task_id: str,
        owner_id: str,
    ) -> ExportSourceSnapshot: ...


class ExportApplicationService:
    def __init__(
        self,
        document_source: ExportDocumentSource,
        store,
        gateway: DocumentExportGateway,
        *,
        confirmation_secret: bytes,
        formatter: FeishuExportFormatter | None = None,
        intent_ttl_seconds: int = 600,
        now=None,
        event_sink=None,
    ) -> None:
        if len(confirmation_secret) < 16:
            raise ValueError("confirmation_secret must be at least 16 bytes")
        self.document_source = document_source
        self.store = store
        self.gateway = gateway
        self.confirmation_secret = confirmation_secret
        self.formatter = formatter or FeishuExportFormatter()
        self.intent_ttl_seconds = intent_ttl_seconds
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.event_sink = event_sink or getattr(
            document_source,
            "workflow_repository",
            None,
        )

    def preview(
        self,
        *,
        task_id: str,
        owner_id: str,
        mode: ExportMode,
        expected_task_version: int,
        idempotency_key: str | None = None,
    ) -> ExportPreview:
        input_hash = sha256_json(
            {
                "task_id": task_id,
                "owner_id": owner_id,
                "mode": mode,
                "expected_task_version": expected_task_version,
            }
        )
        if idempotency_key:
            get_replay = getattr(self.store, "get_preview_replay", None)
            replay = get_replay(owner_id, idempotency_key) if get_replay else None
            if replay:
                replay_hash, intent = replay
                if replay_hash != input_hash:
                    raise IntegrationError(
                        IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency key was used with different preview input",
                    )
                return self._preview_from_intent(intent)
        source = self._current_source(task_id, owner_id, expected_task_version)
        document = self._format(source)
        binding = self.store.get_binding(task_id, owner_id)
        if mode == ExportMode.CREATE and binding is not None:
            raise IntegrationError(
                IntegrationErrorCode.INVALID_STATE,
                "task already has a bound document",
            )
        if mode == ExportMode.OVERWRITE_BOUND and binding is None:
            raise IntegrationError(
                IntegrationErrorCode.INVALID_STATE,
                "task has no bound document",
            )
        intent = ExportIntent(
            intent_id=f"export-intent-{uuid.uuid4().hex}",
            task_id=task_id,
            owner_id=owner_id,
            mode=mode,
            task_version=source.task_version,
            document_id=source.document_id,
            document_version=source.document_version,
            content_hash=document.content_hash,
            title=document.title,
            unresolved_items=document.unresolved_items,
            bound_title=binding.display_title if binding else None,
            bound_safe_url=binding.safe_url if binding else None,
            expires_at=self.now() + timedelta(seconds=self.intent_ttl_seconds),
        )
        if idempotency_key:
            begin_preview = getattr(self.store, "begin_preview", None)
            if begin_preview is not None:
                concurrent_replay = begin_preview(
                    owner_id,
                    idempotency_key,
                    input_hash,
                    intent,
                )
                if concurrent_replay is not None:
                    return self._preview_from_intent(concurrent_replay)
            else:
                self.store.save_intent(intent)
                save_replay = getattr(self.store, "save_preview_replay", None)
                if save_replay:
                    save_replay(
                        owner_id,
                        idempotency_key,
                        input_hash,
                        intent,
                    )
        else:
            self.store.save_intent(intent)
        self._emit(
            intent.task_id,
            "ExportPreviewed",
            {
                "intent_id": intent.intent_id,
                "mode": intent.mode.value,
                "document_version": intent.document_version,
                "content_hash": intent.content_hash,
            },
        )
        return self._preview_from_intent(intent)

    def _preview_from_intent(self, intent: ExportIntent) -> ExportPreview:
        return ExportPreview(
            intent_id=intent.intent_id,
            confirmation_token=self._confirmation_token(intent),
            mode=intent.mode,
            title=intent.title,
            task_version=intent.task_version,
            document_version=intent.document_version,
            content_hash=intent.content_hash,
            unresolved_items=intent.unresolved_items,
            expires_at=intent.expires_at,
            bound_title=intent.bound_title,
            bound_safe_url=intent.bound_safe_url,
        )

    def execute(
        self,
        *,
        intent_id: str,
        confirmation_token: str,
        owner_id: str,
        expected_task_version: int,
        idempotency_key: str,
        mode: ExportMode | None = None,
        expected_task_id: str | None = None,
    ) -> ExportRun:
        input_hash = sha256_json(
            {
                "intent_id": intent_id,
                "owner_id": owner_id,
                "expected_task_version": expected_task_version,
                "expected_task_id": expected_task_id,
            }
        )
        replay = self.store.get_replay(owner_id, idempotency_key)
        if replay:
            replay_hash, run = replay
            if replay_hash != input_hash:
                raise IntegrationError(
                    IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                    "idempotency key was used with different input",
                )
            return run

        intent = self.store.get_intent(intent_id, owner_id)
        if expected_task_id is not None and intent.task_id != expected_task_id:
            raise IntegrationError(
                IntegrationErrorCode.RESOURCE_NOT_FOUND,
                "export intent was not found",
            )
        if mode is not None and ExportMode(mode) != intent.mode:
            raise IntegrationError(
                IntegrationErrorCode.CONFIRMATION_REQUIRED,
                "export mode does not match the confirmed preview",
            )
        self._verify_confirmation(intent, confirmation_token)
        if intent.consumed_at is not None:
            raise IntegrationError(
                IntegrationErrorCode.CONFIRMATION_REQUIRED,
                "export confirmation was already used",
            )
        if self.now() >= intent.expires_at:
            raise IntegrationError(
                IntegrationErrorCode.CONFIRMATION_REQUIRED,
                "export confirmation expired",
            )
        if expected_task_version != intent.task_version:
            raise IntegrationError(
                IntegrationErrorCode.RESOURCE_CHANGED,
                "task version changed after preview",
            )
        source = self._current_source(
            intent.task_id,
            owner_id,
            expected_task_version,
        )
        document = self._format(source)
        if (
            source.document_id != intent.document_id
            or source.document_version != intent.document_version
            or document.content_hash != intent.content_hash
        ):
            raise IntegrationError(
                IntegrationErrorCode.RESOURCE_CHANGED,
                "document changed after preview",
            )
        binding = self.store.get_binding(intent.task_id, owner_id)
        if intent.mode == ExportMode.CREATE and binding is not None:
            raise IntegrationError(
                IntegrationErrorCode.INVALID_STATE,
                "task already has a bound document",
            )
        if intent.mode == ExportMode.OVERWRITE_BOUND and binding is None:
            raise IntegrationError(
                IntegrationErrorCode.INVALID_STATE,
                "task has no bound document",
            )
        run = ExportRun(
            export_run_id=f"export-run-{uuid.uuid4().hex}",
            intent_id=intent.intent_id,
            task_id=intent.task_id,
            owner_id=owner_id,
            mode=intent.mode,
            task_version=intent.task_version,
            document_version=intent.document_version,
            content_hash=intent.content_hash,
            status=ExportRunStatus.RUNNING,
            idempotency_key_hash=sha256_json({"idempotency_key": idempotency_key}),
        )
        begin_run = getattr(self.store, "begin_run", None)
        if begin_run is not None:
            concurrent_replay = begin_run(
                owner_id,
                idempotency_key,
                input_hash,
                run,
            )
            if concurrent_replay is not None:
                return concurrent_replay
        else:
            self.store.save_run(run)
        self._emit(
            run.task_id,
            "ExportStarted",
            {
                "export_run_id": run.export_run_id,
                "mode": run.mode.value,
                "document_version": run.document_version,
            },
        )
        try:
            if intent.mode == ExportMode.CREATE:
                result = self.gateway.create_document(
                    document,
                    idempotency_key=idempotency_key,
                )
                binding = ExternalDocumentBinding(
                    binding_id=f"document-binding-{uuid.uuid4().hex}",
                    task_id=intent.task_id,
                    owner_id=owner_id,
                    external_id=result.external_id,
                    safe_url=result.safe_url,
                    display_title=result.title,
                    last_export_hash=document.content_hash,
                    last_document_version=document.document_version,
                    provider_revision=result.provider_revision,
                )
            else:
                assert binding is not None
                result = self.gateway.overwrite_document(
                    binding.external_id,
                    document,
                    idempotency_key=idempotency_key,
                    expected_revision=binding.provider_revision,
                )
                binding = binding.model_copy(
                    update={
                        "safe_url": result.safe_url,
                        "display_title": result.title,
                        "last_export_hash": document.content_hash,
                        "last_document_version": document.document_version,
                        "provider_revision": result.provider_revision,
                    }
                )
            self.store.save_binding(binding)
            run = run.model_copy(
                update={
                    "status": ExportRunStatus.SUCCEEDED,
                    "binding_id": binding.binding_id,
                    "safe_url": binding.safe_url,
                    "display_title": binding.display_title,
                    "completed_at": self.now(),
                }
            )
        except IntegrationError as exc:
            status = ExportRunStatus.FAILED
            if exc.code == IntegrationErrorCode.RESULT_UNKNOWN:
                status = (
                    ExportRunStatus.MANUAL_REVIEW
                    if self.gateway.create_idempotency_capability
                    == CreateIdempotencyCapability.NONE
                    else ExportRunStatus.RESULT_UNKNOWN
                )
            run = run.model_copy(
                update={
                    "status": status,
                    "error_code": exc.code.value,
                    "retryable": exc.retryable and status != ExportRunStatus.MANUAL_REVIEW,
                    "completed_at": self.now(),
                }
            )
        self.store.consume_intent(
            intent.model_copy(update={"consumed_at": self.now()})
        )
        self.store.save_replay(
            owner_id,
            idempotency_key,
            input_hash,
            run,
        )
        self._emit(
            run.task_id,
            "ExportSucceeded"
            if run.status == ExportRunStatus.SUCCEEDED
            else "ExportFailed",
            {
                "export_run_id": run.export_run_id,
                "mode": run.mode.value,
                "document_version": run.document_version,
                "binding_id": run.binding_id,
                "status": run.status.value,
                "error_code": run.error_code,
                "retryable": run.retryable,
            },
        )
        return run

    def list_exports(self, task_id: str, owner_id: str) -> tuple[ExportRun, ...]:
        self.document_source.get_export_source(task_id, owner_id)
        return self.store.list_runs(task_id, owner_id)

    def _current_source(
        self,
        task_id: str,
        owner_id: str,
        expected_task_version: int,
    ) -> ExportSourceSnapshot:
        source = self.document_source.get_export_source(task_id, owner_id)
        if source.task_status != "COMPLETED":
            raise IntegrationError(
                IntegrationErrorCode.INVALID_STATE,
                "only completed tasks can be exported",
            )
        if source.task_version != expected_task_version:
            raise IntegrationError(
                IntegrationErrorCode.RESOURCE_CHANGED,
                "task version changed",
            )
        return source

    def _format(self, source: ExportSourceSnapshot):
        return self.formatter.format(
            source.markdown,
            document_version=source.document_version,
            unresolved_items=source.unresolved_items,
        )

    def _confirmation_token(self, intent: ExportIntent) -> str:
        payload = canonical_json(
            {
                "intent_id": intent.intent_id,
                "task_id": intent.task_id,
                "owner_id": intent.owner_id,
                "mode": intent.mode,
                "task_version": intent.task_version,
                "document_version": intent.document_version,
                "content_hash": intent.content_hash,
                "expires_at": intent.expires_at.isoformat(),
            }
        ).encode("utf-8")
        return hmac.new(self.confirmation_secret, payload, hashlib.sha256).hexdigest()

    def _verify_confirmation(self, intent: ExportIntent, supplied: str) -> None:
        if not hmac.compare_digest(self._confirmation_token(intent), supplied):
            raise IntegrationError(
                IntegrationErrorCode.CONFIRMATION_REQUIRED,
                "export confirmation is invalid",
            )

    def _emit(self, task_id: str, event_type: str, payload: dict) -> None:
        append = getattr(self.event_sink, "append_event", None)
        if append is not None:
            append(task_id, event_type, payload)
