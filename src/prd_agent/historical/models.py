from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CorpusStatus(StrEnum):
    BUILDING = "BUILDING"
    READY = "READY"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


class DocumentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    DELETED = "DELETED"


class RetrievalMode(StrEnum):
    KEYWORD = "KEYWORD"
    HYBRID = "HYBRID"


class RetrievalStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    EMPTY = "EMPTY"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class HistoricalCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    corpus_version: str = Field(pattern=r"^corpus-[0-9a-f]{32}$")
    manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    document_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    tokenizer_version: str = Field(min_length=1)
    embedding_model_id: str | None = None
    status: CorpusStatus
    created_at: datetime = Field(default_factory=utc_now)


class HistoricalPrdDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_uri: str = Field(min_length=1)
    access_labels: tuple[str, ...] = ()
    product_tags: tuple[str, ...] = ()
    created_at_source: datetime | None = None
    updated_at_source: datetime | None = None
    status: DocumentStatus = DocumentStatus.ACTIVE


class HistoricalPrdVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_version_id: str = Field(pattern=r"^docver-[0-9a-f]{32}$")
    document_id: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    markdown: str
    imported_at: datetime = Field(default_factory=utc_now)
    supersedes_version_id: str | None = None


class HistoricalPrdChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str = Field(pattern=r"^chunk-[0-9a-f]{32}$")
    document_version_id: str
    section_path: tuple[str, ...]
    ordinal: int = Field(ge=0)
    content: str = Field(min_length=1, max_length=1200)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    token_text: str
    token_count: int = Field(ge=0)
    embedding: tuple[float, ...] | None = None


class HistoricalPrdSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1, max_length=500)
    product_tags: tuple[str, ...] = ()
    updated_after: datetime | None = None
    mode: RetrievalMode = RetrievalMode.KEYWORD
    limit: int = Field(default=5, ge=1, le=5)


class RetrievalRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    retrieval_run_id: str = Field(min_length=1)
    investigation_id: str = Field(min_length=1)
    corpus_id: str = Field(min_length=1)
    corpus_version: str = Field(min_length=1)
    query_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    mode: RetrievalMode
    filters_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    limit: int = Field(ge=1, le=5)
    duration_ms: int = Field(ge=0)
    status: RetrievalStatus
    fallback_mode: RetrievalMode | None = None
    created_at: datetime = Field(default_factory=utc_now)


class RetrievalHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    retrieval_run_id: str
    chunk_id: str
    rank: int = Field(ge=1, le=5)
    keyword_rank: int | None = Field(default=None, ge=1)
    vector_rank: int | None = Field(default=None, ge=1)
    fused_score: float | None = None
    stale_hint: bool = False


class HistoricalPrdResultItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    document_version_id: str
    chunk_id: str
    title: str
    section_path: tuple[str, ...]
    source_uri: str
    updated_at_source: datetime | None
    excerpt: str = Field(max_length=800)
    content_hash: str
    stale_hint: bool = False


class HistoricalRetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run: RetrievalRun
    hits: tuple[RetrievalHit, ...] = ()
    items: tuple[HistoricalPrdResultItem, ...] = ()
    public_summary: str
    error_code: Literal["HYBRID_UNAVAILABLE"] | None = None
