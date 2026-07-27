from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from prd_agent.repository.bindings import normalize_relative_path
from prd_agent.repository.content_policy import RepositoryContentPolicy
from prd_agent.repository.errors import BinaryFileBlocked, FileTooLarge
from prd_agent.repository.errors import BlockedPath
from prd_agent.repository.path_policy import RepositoryPathPolicy
from prd_agent.tools.models import ToolResult, ToolResultItem, ToolStatus, TruncationInfo
from prd_agent.tools.policies import ToolLimitPolicy


class SearchTextArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1, max_length=200)
    prefix: str = ""
    case_sensitive: bool = True
    extensions: tuple[str, ...] = ()

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query cannot be blank")
        return value

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        return normalize_relative_path(value, allow_empty=True)


class SearchTextTool:
    tool_id = "search_text"
    schema_version = "1"

    def __init__(self, reader, limits: ToolLimitPolicy) -> None:
        self.reader = reader
        self.limits = limits
        self.path_policy = RepositoryPathPolicy()
        self.content_policy = RepositoryContentPolicy()

    def execute(self, snapshot, arguments: SearchTextArguments) -> ToolResult:
        candidates = []
        try:
            prefix = self.path_policy.validate(arguments.prefix, allow_empty=True).rstrip("/")
        except BlockedPath as exc:
            return ToolResult(
                status=ToolStatus.BLOCKED,
                public_summary="文本搜索路径被安全策略阻止",
                error_code=exc.code,
            )
        for entry in self.reader.list_blobs(snapshot):
            if entry.mode == "120000" or not self.path_policy.allowed(entry.path):
                continue
            if prefix and entry.path != prefix and not entry.path.startswith(prefix + "/"):
                continue
            if arguments.extensions and not entry.path.endswith(arguments.extensions):
                continue
            candidates.append(entry)

        partial_reason: str | None = None
        if len(candidates) > self.limits.max_search_candidate_files:
            candidates = candidates[: self.limits.max_search_candidate_files]
            partial_reason = "CANDIDATE_FILE_LIMIT"

        scanned = 0
        items: list[ToolResultItem] = []
        needle = arguments.query if arguments.case_sensitive else arguments.query.lower()
        for entry in candidates:
            if scanned + entry.size > self.limits.max_search_scanned_bytes:
                partial_reason = partial_reason or "SCANNED_BYTE_LIMIT"
                break
            scanned += entry.size
            try:
                blob = self.reader.read_blob(snapshot, entry.path)
                text = self.content_policy.decode_text(
                    blob.data,
                    max_bytes=self.limits.max_search_scanned_bytes,
                )
            except (BinaryFileBlocked, FileTooLarge):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                haystack = line if arguments.case_sensitive else line.lower()
                start = haystack.find(needle)
                if start < 0:
                    continue
                excerpt, redacted = self.content_policy.redact(line)
                items.append(
                    ToolResultItem(
                        kind="text_match",
                        path=entry.path,
                        line_start=line_number,
                        line_end=line_number,
                        column=start + 1,
                        excerpt=excerpt,
                        blob_id=entry.blob_id,
                        metadata={"redaction_applied": redacted},
                    )
                )
                if len(items) >= self.limits.max_search_results:
                    partial_reason = "RESULT_LIMIT"
                    break
            if partial_reason == "RESULT_LIMIT":
                break

        status = (
            ToolStatus.PARTIAL
            if partial_reason
            else (ToolStatus.SUCCEEDED if items else ToolStatus.EMPTY)
        )
        return ToolResult(
            status=status,
            items=tuple(items),
            truncation=(
                TruncationInfo(
                    reason=partial_reason,
                    limit=(
                        self.limits.max_search_results
                        if partial_reason == "RESULT_LIMIT"
                        else self.limits.max_search_scanned_bytes
                    ),
                    returned=len(items),
                )
                if partial_reason
                else None
            ),
            public_summary=f"固定文本搜索返回 {len(items)} 个匹配位置",
        )
