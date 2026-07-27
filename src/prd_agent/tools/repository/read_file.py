from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from prd_agent.repository.bindings import normalize_relative_path
from prd_agent.repository.content_policy import RepositoryContentPolicy
from prd_agent.repository.errors import RepositoryError
from prd_agent.repository.path_policy import RepositoryPathPolicy
from prd_agent.tools.models import ToolResult, ToolResultItem, ToolStatus
from prd_agent.tools.policies import ToolLimitPolicy


class ReadFileArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_path(value)

    @model_validator(mode="after")
    def validate_range(self):
        if self.line_end < self.line_start:
            raise ValueError("line_end must be greater than or equal to line_start")
        return self


class ReadFileTool:
    tool_id = "read_file"
    schema_version = "1"

    def __init__(self, reader, limits: ToolLimitPolicy) -> None:
        self.reader = reader
        self.limits = limits
        self.path_policy = RepositoryPathPolicy()
        self.content_policy = RepositoryContentPolicy()

    def execute(self, snapshot, arguments: ReadFileArguments) -> ToolResult:
        requested_lines = arguments.line_end - arguments.line_start + 1
        if requested_lines > self.limits.max_read_lines:
            return ToolResult(
                status=ToolStatus.BLOCKED,
                public_summary="读取范围超过单次行数限制",
                error_code="READ_RANGE_LIMIT",
            )
        try:
            path = self.path_policy.validate(arguments.path)
            blob = self.reader.read_blob(snapshot, path)
            text = self.content_policy.decode_text(
                blob.data,
                max_bytes=self.limits.max_parser_input_bytes,
            )
        except RepositoryError as exc:
            return ToolResult(
                status=ToolStatus.BLOCKED,
                public_summary="文件读取被仓库策略拒绝",
                error_code=exc.code,
            )
        lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        if arguments.line_start > len(lines):
            return ToolResult(
                status=ToolStatus.EMPTY,
                public_summary="指定行范围没有内容",
            )
        actual_end = min(arguments.line_end, len(lines))
        excerpt = "\n".join(lines[arguments.line_start - 1 : actual_end])
        encoded = excerpt.encode("utf-8")
        if len(encoded) > self.limits.max_excerpt_bytes:
            return ToolResult(
                status=ToolStatus.BLOCKED,
                public_summary="读取片段超过单次字节限制",
                error_code="EXCERPT_BYTE_LIMIT",
            )
        excerpt, redacted = self.content_policy.redact(excerpt)
        return ToolResult(
            status=ToolStatus.SUCCEEDED,
            items=(
                ToolResultItem(
                    kind="file_excerpt",
                    path=path,
                    line_start=arguments.line_start,
                    line_end=actual_end,
                    excerpt=excerpt,
                    blob_id=blob.blob_id,
                    metadata={"redaction_applied": redacted},
                ),
            ),
            public_summary=f"读取 {path} 第 {arguments.line_start}-{actual_end} 行",
        )
