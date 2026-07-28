from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from prd_agent.repository.bindings import normalize_relative_path
from prd_agent.repository.content_policy import RepositoryContentPolicy
from prd_agent.repository.errors import BinaryFileBlocked, FileTooLarge
from prd_agent.tools.models import ToolResult, ToolResultItem, ToolStatus, TruncationInfo
from prd_agent.tools.policies import ToolLimitPolicy


class FindReferencesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=200)
    prefix: str = ""
    exclude_definitions: bool = True
    extensions: tuple[str, ...] = ()

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        return normalize_relative_path(value, allow_empty=True)


class FindReferencesTool:
    tool_id = "find_references"
    schema_version = "1"

    def __init__(self, reader, limits: ToolLimitPolicy) -> None:
        self.reader = reader
        self.limits = limits
        self.content_policy = RepositoryContentPolicy()

    def execute(self, snapshot, arguments: FindReferencesArguments) -> ToolResult:
        prefix = arguments.prefix.rstrip("/")
        escaped = re.escape(arguments.symbol)
        reference = re.compile(rf"\b{escaped}\b")
        definition = re.compile(
            rf"^\s*(?:(?:async\s+)?def|class|function|interface|type|const|let|var)\s+{escaped}\b|^\s*{escaped}\s*="
        )
        items = []
        scanned = 0
        partial_reason = None
        candidates = [
            entry
            for entry in self.reader.list_blobs(snapshot)
            if (not prefix or entry.path == prefix or entry.path.startswith(prefix + "/"))
            and (not arguments.extensions or entry.path.endswith(arguments.extensions))
        ]
        if len(candidates) > self.limits.max_search_candidate_files:
            candidates = candidates[: self.limits.max_search_candidate_files]
            partial_reason = "CANDIDATE_FILE_LIMIT"
        for entry in candidates:
            if scanned + entry.size > self.limits.max_search_scanned_bytes:
                partial_reason = partial_reason or "SCANNED_BYTE_LIMIT"
                break
            scanned += entry.size
            try:
                blob = self.reader.read_blob(snapshot, entry.path)
                text = self.content_policy.decode_text(
                    blob.data, max_bytes=self.limits.max_search_scanned_bytes
                )
            except (BinaryFileBlocked, FileTooLarge):
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                match = reference.search(line)
                if not match or (arguments.exclude_definitions and definition.search(line)):
                    continue
                excerpt, redacted = self.content_policy.redact(line)
                items.append(
                    ToolResultItem(
                        kind="symbol_reference",
                        path=entry.path,
                        line_start=line_no,
                        line_end=line_no,
                        column=match.start() + 1,
                        excerpt=excerpt,
                        blob_id=entry.blob_id,
                        metadata={
                            "symbol": arguments.symbol,
                            "redaction_applied": redacted,
                        },
                    )
                )
                if len(items) >= self.limits.max_search_results:
                    partial_reason = "RESULT_LIMIT"
                    break
            if partial_reason == "RESULT_LIMIT":
                break
        status = ToolStatus.PARTIAL if partial_reason else (
            ToolStatus.SUCCEEDED if items else ToolStatus.EMPTY
        )
        return ToolResult(
            status=status,
            items=tuple(items),
            truncation=(
                TruncationInfo(
                    reason=partial_reason,
                    limit=self.limits.max_search_results,
                    returned=len(items),
                )
                if partial_reason
                else None
            ),
            public_summary=f"找到 {len(items)} 个 Symbol 引用",
        )
