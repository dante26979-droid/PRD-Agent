from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    EMPTY = "EMPTY"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class ToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str = Field(min_length=1)
    tool_schema_version: str = Field(min_length=1)
    repository_id: str = Field(min_length=1)
    resolved_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    arguments: dict[str, Any]
    purpose: str = Field(min_length=1, max_length=500)


class ToolResultItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    path: str
    line_start: int | None = None
    line_end: int | None = None
    column: int | None = None
    excerpt: str | None = None
    blob_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TruncationInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    limit: int
    returned: int


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ToolStatus
    items: tuple[ToolResultItem, ...] = ()
    truncation: TruncationInfo | None = None
    public_summary: str
    error_code: str | None = None
    duration_ms: int = 0
