from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator
import yaml

from prd_agent.repository.bindings import normalize_relative_path
from prd_agent.repository.content_policy import RepositoryContentPolicy
from prd_agent.repository.errors import RepositoryError
from prd_agent.repository.path_policy import RepositoryPathPolicy
from prd_agent.tools.models import ToolResult, ToolResultItem, ToolStatus
from prd_agent.tools.policies import ToolLimitPolicy


class ParseOpenApiArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    operation: str | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_path(value)


class ParseOpenApiTool:
    tool_id = "parse_openapi"
    schema_version = "1"

    def __init__(self, reader, limits: ToolLimitPolicy) -> None:
        self.reader = reader
        self.limits = limits
        self.path_policy = RepositoryPathPolicy()
        self.content_policy = RepositoryContentPolicy()

    def execute(self, snapshot, arguments: ParseOpenApiArguments) -> ToolResult:
        try:
            path = self.path_policy.validate(arguments.path)
            blob = self.reader.read_blob(snapshot, path)
            text = self.content_policy.decode_text(
                blob.data, max_bytes=self.limits.max_parser_input_bytes
            )
            document = yaml.safe_load(text)
        except (RepositoryError, yaml.YAMLError) as exc:
            code = exc.code if isinstance(exc, RepositoryError) else "MALFORMED_OPENAPI"
            return ToolResult(
                status=ToolStatus.FAILED,
                public_summary="OpenAPI 文档读取或解析失败",
                error_code=code,
            )
        if not isinstance(document, dict) or not str(document.get("openapi", "")).startswith("3."):
            return ToolResult(
                status=ToolStatus.FAILED,
                public_summary="仅支持 OpenAPI 3.x 文档",
                error_code="UNSUPPORTED_OPENAPI_VERSION",
            )
        if self._has_external_ref(document):
            return ToolResult(
                status=ToolStatus.BLOCKED,
                public_summary="OpenAPI 外部引用被安全策略阻止",
                error_code="BLOCKED_EXTERNAL_REFERENCE",
            )

        operation_filter = None
        if arguments.operation:
            parts = arguments.operation.split(maxsplit=1)
            operation_filter = (
                f"{parts[0].upper()} {parts[1]}" if len(parts) == 2 else arguments.operation
            )
        items: list[ToolResultItem] = []
        for route, path_item in sorted((document.get("paths") or {}).items()):
            if not isinstance(path_item, dict):
                continue
            for method in ("get", "post", "put", "patch", "delete"):
                operation = path_item.get(method)
                operation_name = f"{method.upper()} {route}"
                if not isinstance(operation, dict) or (
                    operation_filter and operation_name != operation_filter
                ):
                    continue
                schema = self._request_schema(document, operation)
                if not isinstance(schema, dict):
                    continue
                required = set(schema.get("required") or [])
                for field, raw_field_schema in sorted((schema.get("properties") or {}).items()):
                    field_schema = self._resolve_local_ref(document, raw_field_schema)
                    if not isinstance(field_schema, dict):
                        continue
                    line_start, line_end, excerpt = self._locate(text, str(field))
                    metadata = {
                        "operation": operation_name,
                        "field": str(field),
                        "required": field in required,
                    }
                    for name in (
                        "type",
                        "format",
                        "enum",
                        "nullable",
                        "minimum",
                        "maximum",
                        "minLength",
                        "maxLength",
                        "pattern",
                    ):
                        if name in field_schema:
                            metadata[name] = field_schema[name]
                    items.append(
                        ToolResultItem(
                            kind="openapi_field",
                            path=path,
                            line_start=line_start,
                            line_end=line_end,
                            excerpt=excerpt,
                            blob_id=blob.blob_id,
                            metadata=metadata,
                        )
                    )
        return ToolResult(
            status=ToolStatus.SUCCEEDED if items else ToolStatus.EMPTY,
            items=tuple(items),
            public_summary=f"OpenAPI Parser 返回 {len(items)} 个字段契约",
        )

    @staticmethod
    def _request_schema(document: dict[str, Any], operation: dict[str, Any]) -> Any:
        body = operation.get("requestBody") or {}
        if isinstance(body, dict) and "$ref" in body:
            body = ParseOpenApiTool._resolve_local_ref(document, body)
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, dict) or not content:
            return None
        media = content.get("application/json") or next(iter(content.values()))
        schema = media.get("schema") if isinstance(media, dict) else None
        return ParseOpenApiTool._resolve_local_ref(document, schema)

    @staticmethod
    def _resolve_local_ref(document: dict[str, Any], value: Any) -> Any:
        if not isinstance(value, dict) or "$ref" not in value:
            return value
        reference = str(value["$ref"])
        if not reference.startswith("#/"):
            return value
        current: Any = document
        for part in reference[2:].split("/"):
            if not isinstance(current, dict):
                return value
            current = current.get(part.replace("~1", "/").replace("~0", "~"))
        return current

    @staticmethod
    def _has_external_ref(value: Any) -> bool:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and not reference.startswith("#/"):
                return True
            return any(ParseOpenApiTool._has_external_ref(item) for item in value.values())
        if isinstance(value, list):
            return any(ParseOpenApiTool._has_external_ref(item) for item in value)
        return False

    @staticmethod
    def _locate(text: str, field: str) -> tuple[int, int, str]:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if f'"{field}"' in line or f"{field}:" in line:
                start = index
                stripped = line.strip()
                if "{" in stripped:
                    depth = 0
                    end = index
                    for candidate in range(index, len(lines)):
                        depth += lines[candidate].count("{")
                        depth -= lines[candidate].count("}")
                        end = candidate
                        if depth <= 0:
                            break
                else:
                    indent = len(line) - len(line.lstrip())
                    end = index
                    for candidate in range(index + 1, len(lines)):
                        next_line = lines[candidate]
                        if not next_line.strip():
                            continue
                        next_indent = len(next_line) - len(next_line.lstrip())
                        if next_indent <= indent:
                            break
                        end = candidate
                excerpt = "\n".join(lines[start : end + 1]).strip().rstrip(",")
                return start + 1, end + 1, excerpt
        fallback = lines[0].strip() if lines else ""
        return 1, 1, fallback
