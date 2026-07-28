from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ExportBlockType(StrEnum):
    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    ORDERED_LIST = "ORDERED_LIST"
    UNORDERED_LIST = "UNORDERED_LIST"
    TABLE = "TABLE"
    QUOTE = "QUOTE"
    CODE = "CODE"
    DIVIDER = "DIVIDER"


class ExportMode(StrEnum):
    CREATE = "CREATE"
    OVERWRITE_BOUND = "OVERWRITE_BOUND"


class ExportRunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class CreateIdempotencyCapability(StrEnum):
    PROVIDER_KEY = "PROVIDER_KEY"
    RECONCILABLE = "RECONCILABLE"
    NONE = "NONE"


class ExportBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    block_type: ExportBlockType
    text: str | None = None
    level: int | None = Field(default=None, ge=1, le=3)
    items: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    language: str | None = None


class ExportDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=200)
    document_version: int = Field(ge=1)
    blocks: tuple[ExportBlock, ...]
    unresolved_items: tuple[str, ...] = ()
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ExportSourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    owner_id: str
    task_status: str
    task_version: int = Field(ge=1)
    document_id: str
    document_version: int = Field(ge=1)
    markdown: str
    unresolved_items: tuple[str, ...] = ()


class ExportIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str
    task_id: str
    owner_id: str
    mode: ExportMode
    task_version: int
    document_id: str
    document_version: int
    content_hash: str
    title: str
    unresolved_items: tuple[str, ...] = ()
    bound_title: str | None = None
    bound_safe_url: str | None = None
    expires_at: datetime
    consumed_at: datetime | None = None


class ExportPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str
    confirmation_token: str
    mode: ExportMode
    title: str
    task_version: int
    document_version: int
    content_hash: str
    unresolved_items: tuple[str, ...] = ()
    expires_at: datetime
    bound_title: str | None = None
    bound_safe_url: str | None = None


class ProviderDocumentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    safe_url: str
    title: str
    provider_revision: str | None = None


class ExternalDocumentBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_id: str
    task_id: str
    owner_id: str
    provider: str = "FEISHU"
    external_id: str
    safe_url: str
    display_title: str
    last_export_hash: str
    last_document_version: int
    provider_revision: str | None = None


class ExportRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    export_run_id: str
    intent_id: str
    task_id: str
    owner_id: str
    mode: ExportMode
    task_version: int
    document_version: int
    content_hash: str
    status: ExportRunStatus
    idempotency_key_hash: str
    attempt_count: int = 1
    binding_id: str | None = None
    safe_url: str | None = None
    display_title: str | None = None
    error_code: str | None = None
    retryable: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
