from __future__ import annotations

import hashlib
import uuid

from prd_agent.tools.models import ToolAction, ToolResult, ToolStatus

from .models import (
    ExtractionMethod,
    RepositoryEvidenceBundle,
    SourceEvidence,
    UnknownItem,
    UnknownReason,
)


_METHODS = {
    "repo_tree": ExtractionMethod.TREE,
    "search_text": ExtractionMethod.SEARCH,
    "read_file": ExtractionMethod.SOURCE_READ,
    "parse_openapi": ExtractionMethod.OPENAPI_PARSE,
    "parse_database_schema": ExtractionMethod.DATABASE_SCHEMA_PARSE,
    "find_related_tests": ExtractionMethod.RELATED_TEST_SEARCH,
    "find_symbol": ExtractionMethod.SYMBOL_SEARCH,
    "find_references": ExtractionMethod.REFERENCE_SEARCH,
}


class EvidenceNormalizer:
    def normalize(
        self,
        tool_call_id: str,
        action: ToolAction,
        result: ToolResult,
        *,
        task_id: str | None = None,
    ) -> RepositoryEvidenceBundle:
        evidence = []
        if result.status in {ToolStatus.SUCCEEDED, ToolStatus.PARTIAL}:
            for item in result.items:
                excerpt = (item.excerpt if item.excerpt is not None else item.path).replace(
                    "\r\n", "\n"
                ).replace("\r", "\n")
                content_hash = "sha256:" + hashlib.sha256(
                    excerpt.encode("utf-8")
                ).hexdigest()
                evidence.append(
                    SourceEvidence(
                        evidence_id=f"evidence-{uuid.uuid4().hex}",
                        tool_call_id=tool_call_id,
                        repository_id=action.repository_id,
                        resolved_commit_sha=action.resolved_commit_sha,
                        path=item.path,
                        line_start=item.line_start,
                        line_end=item.line_end,
                        excerpt=excerpt,
                        content_hash=content_hash,
                        source_blob_id=item.blob_id,
                        extraction_method=_METHODS[action.tool_id],
                        redaction_applied=bool(
                            item.metadata.get("redaction_applied", False)
                        ),
                    )
                )
        unknowns = []
        if result.status == ToolStatus.EMPTY:
            unknowns.append(
                UnknownItem(
                    unknown_id=f"unknown-{uuid.uuid4().hex}",
                    tool_call_id=tool_call_id,
                    task_id=task_id,
                    statement="本次固定查询未找到匹配结果，不能据此断言目标信息不存在",
                    reason=UnknownReason.EMPTY_RESULT,
                )
            )
        elif result.status == ToolStatus.PARTIAL:
            unknowns.append(
                UnknownItem(
                    unknown_id=f"unknown-{uuid.uuid4().hex}",
                    tool_call_id=tool_call_id,
                    task_id=task_id,
                    statement="本次查询因资源限制仅返回部分结果",
                    reason=(
                        UnknownReason.PARSE_UNSUPPORTED
                        if result.error_code == "PARSE_UNSUPPORTED"
                        else UnknownReason.PARTIAL_RESULT
                    ),
                )
            )
        elif result.status in {ToolStatus.FAILED, ToolStatus.BLOCKED}:
            unknowns.append(
                UnknownItem(
                    unknown_id=f"unknown-{uuid.uuid4().hex}",
                    tool_call_id=tool_call_id,
                    task_id=task_id,
                    statement="工具未能取得可核查来源",
                    reason=(
                        UnknownReason.ACCESS_BLOCKED
                        if result.status == ToolStatus.BLOCKED
                        else UnknownReason.TOOL_FAILED
                    ),
                )
            )
        return RepositoryEvidenceBundle(
            tool_call_id=tool_call_id,
            evidence=tuple(evidence),
            unknowns=tuple(unknowns),
        )
