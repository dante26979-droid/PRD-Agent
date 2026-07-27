from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from prd_agent.repository.bindings import normalize_relative_path
from prd_agent.repository.content_policy import RepositoryContentPolicy
from prd_agent.repository.errors import BinaryFileBlocked, FileTooLarge
from prd_agent.repository.path_policy import RepositoryPathPolicy
from prd_agent.tools.models import ToolResult, ToolResultItem, ToolStatus, TruncationInfo
from prd_agent.tools.policies import ToolLimitPolicy


class FindRelatedTestsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_path: str
    symbol: str | None = Field(default=None, max_length=200)
    keywords: tuple[str, ...] = ()

    @field_validator("source_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_path(value)


class FindRelatedTestsTool:
    tool_id = "find_related_tests"
    schema_version = "1"

    def __init__(self, reader, limits: ToolLimitPolicy) -> None:
        self.reader = reader
        self.limits = limits
        self.path_policy = RepositoryPathPolicy()
        self.content_policy = RepositoryContentPolicy()

    def execute(self, snapshot, arguments: FindRelatedTestsArguments) -> ToolResult:
        module = PurePosixPath(arguments.source_path).stem.lower()
        ranked: list[tuple[int, ToolResultItem]] = []
        for entry in self.reader.list_blobs(snapshot):
            if not self._is_test_path(entry.path) or not self.path_policy.allowed(entry.path):
                continue
            try:
                blob = self.reader.read_blob(snapshot, entry.path)
                text = self.content_policy.decode_text(
                    blob.data, max_bytes=self.limits.max_search_scanned_bytes
                )
            except (BinaryFileBlocked, FileTooLarge):
                continue
            reason, score, line_no, excerpt = self._score(
                entry.path, text, module, arguments.symbol, arguments.keywords
            )
            if not score:
                continue
            ranked.append(
                (
                    score,
                    ToolResultItem(
                        kind="related_test",
                        path=entry.path,
                        line_start=line_no,
                        line_end=line_no,
                        excerpt=excerpt,
                        blob_id=entry.blob_id,
                        metadata={"score": score, "reason": reason},
                    ),
                )
            )
        ranked.sort(key=lambda value: (-value[0], value[1].path, value[1].line_start or 0))
        truncated = len(ranked) > self.limits.max_related_tests
        selected = ranked[: self.limits.max_related_tests]
        return ToolResult(
            status=(
                ToolStatus.PARTIAL
                if truncated
                else (ToolStatus.SUCCEEDED if selected else ToolStatus.EMPTY)
            ),
            items=tuple(item for _, item in selected),
            truncation=(
                TruncationInfo(
                    reason="RESULT_LIMIT",
                    limit=self.limits.max_related_tests,
                    returned=len(selected),
                )
                if truncated
                else None
            ),
            public_summary=f"找到 {len(selected)} 个相关测试文件",
        )

    @staticmethod
    def _is_test_path(path: str) -> bool:
        posix = PurePosixPath(path)
        name = posix.name.lower()
        return "tests" in (part.lower() for part in posix.parts) or name.startswith(
            "test_"
        ) or "_test." in name

    @staticmethod
    def _score(path: str, text: str, module: str, symbol: str | None, keywords):
        lines = text.splitlines()
        if symbol:
            for number, line in enumerate(lines, start=1):
                if symbol in line:
                    return "symbol", 300, number, line.strip()
        if module and module in path.lower():
            return "module", 200, 1, lines[0].strip() if lines else ""
        for keyword in keywords:
            for number, line in enumerate(lines, start=1):
                if keyword.lower() in line.lower():
                    return "keyword", 100, number, line.strip()
        return "", 0, None, None
