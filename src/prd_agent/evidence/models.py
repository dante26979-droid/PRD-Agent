from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from prd_agent.sources.models import SourceKind


class ExtractionMethod(StrEnum):
    TREE = "TREE"
    SEARCH = "SEARCH"
    SOURCE_READ = "SOURCE_READ"
    OPENAPI_PARSE = "OPENAPI_PARSE"
    DATABASE_SCHEMA_PARSE = "DATABASE_SCHEMA_PARSE"
    RELATED_TEST_SEARCH = "RELATED_TEST_SEARCH"
    SYMBOL_SEARCH = "SYMBOL_SEARCH"
    REFERENCE_SEARCH = "REFERENCE_SEARCH"
    HISTORICAL_RETRIEVAL = "HISTORICAL_RETRIEVAL"
    EXTERNAL_AGENT_CANDIDATE = "EXTERNAL_AGENT_CANDIDATE"


class FactScope(StrEnum):
    CURRENT_STATE = "CURRENT_STATE"
    HISTORICAL_CONTEXT = "HISTORICAL_CONTEXT"
    TARGET_DECISION = "TARGET_DECISION"
    ASSUMPTION = "ASSUMPTION"


class FactType(StrEnum):
    USER_CONFIRMED = "USER_CONFIRMED"
    CODE_VERIFIED = "CODE_VERIFIED"
    DOCUMENT_SUPPORTED = "DOCUMENT_SUPPORTED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class VerificationStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICTING = "CONFLICTING"


class UnknownReason(StrEnum):
    EMPTY_RESULT = "EMPTY_RESULT"
    PARTIAL_RESULT = "PARTIAL_RESULT"
    PARSE_UNSUPPORTED = "PARSE_UNSUPPORTED"
    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    TOOL_FAILED = "TOOL_FAILED"


class SourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    tool_call_id: str
    source_type: str = "CODE"
    source_kind: SourceKind = SourceKind.CODE_REPOSITORY
    source_id: str | None = None
    source_version: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    access_scope_hash: str | None = None
    repository_id: str | None = None
    resolved_commit_sha: str | None = None
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    symbol: str | None = None
    excerpt: str
    content_hash: str
    source_blob_id: str | None = None
    extraction_method: ExtractionMethod
    redaction_applied: bool = False
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="before")
    @classmethod
    def populate_compatible_source_fields(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        kind = data.get("source_kind", SourceKind.CODE_REPOSITORY)
        if kind == SourceKind.CODE_REPOSITORY or kind == SourceKind.CODE_REPOSITORY.value:
            data.setdefault("source_id", data.get("repository_id"))
            data.setdefault("source_version", data.get("resolved_commit_sha"))
            if not data.get("locator") and data.get("path"):
                data["locator"] = {
                    key: data[key]
                    for key in ("path", "line_start", "line_end", "symbol")
                    if data.get(key) is not None
                }
        return data

    @model_validator(mode="after")
    def validate_source_contract(self) -> "SourceEvidence":
        if not self.source_id or not self.source_version:
            raise ValueError("source_id and source_version are required")
        if self.source_kind == SourceKind.CODE_REPOSITORY:
            if not self.repository_id or not self.resolved_commit_sha or not self.path:
                raise ValueError("code evidence requires repository, commit and path")
        elif not self.locator:
            raise ValueError("historical evidence requires a locator")
        return self


class DeterministicFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str
    tool_call_id: str
    task_id: str | None = None
    subject: str
    predicate: str
    value_json: Any
    fact_scope: FactScope = FactScope.CURRENT_STATE
    fact_type: FactType
    confidence: str
    verification_status: VerificationStatus
    extractor_id: str
    extractor_version: str
    evidence_ids: tuple[str, ...]


class UnknownItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    unknown_id: str
    tool_call_id: str
    task_id: str | None = None
    statement: str
    reason: UnknownReason
    severity: str = "MEDIUM"
    resolution_type: str = "RETRY_WITH_DIFFERENT_ACTION"


class SourceConflict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conflict_id: str
    subject: str
    description: str
    fact_ids: tuple[str, ...]
    status: str = "OPEN"


class RepositoryEvidenceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: str
    evidence: tuple[SourceEvidence, ...] = ()
    facts: tuple[DeterministicFact, ...] = ()
    unknowns: tuple[UnknownItem, ...] = ()
    conflicts: tuple[SourceConflict, ...] = ()
