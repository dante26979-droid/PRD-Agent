from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from prd_agent.repository.bindings import normalize_relative_path
from prd_agent.repository.errors import BlockedPath
from prd_agent.repository.path_policy import RepositoryPathPolicy
from prd_agent.tools.models import ToolResult, ToolResultItem, ToolStatus, TruncationInfo
from prd_agent.tools.policies import ToolLimitPolicy


class RepoTreeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prefix: str = ""
    max_depth: int = Field(default=5, ge=1, le=20)
    extensions: tuple[str, ...] = ()

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        return normalize_relative_path(value, allow_empty=True)


class RepoTreeTool:
    tool_id = "repo_tree"
    schema_version = "1"

    def __init__(self, reader, limits: ToolLimitPolicy) -> None:
        self.reader = reader
        self.limits = limits
        self.path_policy = RepositoryPathPolicy()

    def execute(self, snapshot, arguments: RepoTreeArguments) -> ToolResult:
        try:
            prefix = self.path_policy.validate(arguments.prefix, allow_empty=True).rstrip("/")
        except BlockedPath as exc:
            return ToolResult(
                status=ToolStatus.BLOCKED,
                public_summary="仓库目录路径被安全策略阻止",
                error_code=exc.code,
            )
        entries = []
        for entry in self.reader.list_blobs(snapshot):
            if entry.mode == "120000" or not self.path_policy.allowed(entry.path):
                continue
            if prefix and entry.path != prefix and not entry.path.startswith(prefix + "/"):
                continue
            relative_to_prefix = entry.path[len(prefix) + 1 :] if prefix else entry.path
            if len(relative_to_prefix.split("/")) > arguments.max_depth:
                continue
            if arguments.extensions and not entry.path.endswith(arguments.extensions):
                continue
            entries.append(entry)
        truncated = len(entries) > self.limits.max_tree_entries
        selected = entries[: self.limits.max_tree_entries]
        status = ToolStatus.PARTIAL if truncated else (ToolStatus.SUCCEEDED if selected else ToolStatus.EMPTY)
        return ToolResult(
            status=status,
            items=tuple(
                ToolResultItem(
                    kind="file",
                    path=item.path,
                    blob_id=item.blob_id,
                    metadata={"size": item.size, "mode": item.mode},
                )
                for item in selected
            ),
            truncation=(
                TruncationInfo(
                    reason="ENTRY_LIMIT",
                    limit=self.limits.max_tree_entries,
                    returned=len(selected),
                )
                if truncated
                else None
            ),
            public_summary=f"仓库目录返回 {len(selected)} 个文件条目",
        )
