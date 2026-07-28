from __future__ import annotations

from typing import Protocol

from .models import (
    CreateIdempotencyCapability,
    ExportDocument,
    ProviderDocumentResult,
)


class DocumentExportGateway(Protocol):
    create_idempotency_capability: CreateIdempotencyCapability

    def create_document(
        self,
        document: ExportDocument,
        *,
        idempotency_key: str,
    ) -> ProviderDocumentResult: ...

    def overwrite_document(
        self,
        external_id: str,
        document: ExportDocument,
        *,
        idempotency_key: str,
        expected_revision: str | None,
    ) -> ProviderDocumentResult: ...
