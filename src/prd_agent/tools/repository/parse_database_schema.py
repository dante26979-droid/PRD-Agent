from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator
import sqlglot
from sqlglot import exp

from prd_agent.repository.bindings import normalize_relative_path
from prd_agent.repository.content_policy import RepositoryContentPolicy
from prd_agent.repository.errors import RepositoryError
from prd_agent.repository.path_policy import RepositoryPathPolicy
from prd_agent.tools.models import ToolResult, ToolResultItem, ToolStatus, TruncationInfo
from prd_agent.tools.policies import ToolLimitPolicy


class ParseDatabaseSchemaArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    table: str | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_relative_path(value)


class ParseDatabaseSchemaTool:
    tool_id = "parse_database_schema"
    schema_version = "1"

    def __init__(self, reader, limits: ToolLimitPolicy) -> None:
        self.reader = reader
        self.limits = limits
        self.path_policy = RepositoryPathPolicy()
        self.content_policy = RepositoryContentPolicy()

    def execute(self, snapshot, arguments: ParseDatabaseSchemaArguments) -> ToolResult:
        try:
            path = self.path_policy.validate(arguments.path)
            blob = self.reader.read_blob(snapshot, path)
            text = self.content_policy.decode_text(
                blob.data, max_bytes=self.limits.max_parser_input_bytes
            )
            statements = sqlglot.parse(text, read="postgres")
        except (RepositoryError, sqlglot.errors.ParseError) as exc:
            code = exc.code if isinstance(exc, RepositoryError) else "MALFORMED_DDL"
            return ToolResult(
                status=ToolStatus.FAILED,
                public_summary="PostgreSQL DDL 读取或解析失败",
                error_code=code,
            )

        items: list[ToolResultItem] = []
        unsupported = 0
        for statement in statements:
            if not isinstance(statement, exp.Create) or not isinstance(statement.this, exp.Schema):
                unsupported += 1
                continue
            schema = statement.this
            table_name = schema.this.name
            if arguments.table and table_name != arguments.table:
                continue
            for column in schema.expressions:
                if not isinstance(column, exp.ColumnDef):
                    unsupported += 1
                    continue
                metadata = self._column_metadata(table_name, column)
                line_start, line_end, excerpt = self._locate(text, column.name)
                items.append(
                    ToolResultItem(
                        kind="database_column",
                        path=path,
                        line_start=line_start,
                        line_end=line_end,
                        excerpt=excerpt,
                        blob_id=blob.blob_id,
                        metadata=metadata,
                    )
                )

        status = (
            ToolStatus.PARTIAL
            if unsupported and items
            else (ToolStatus.SUCCEEDED if items else ToolStatus.EMPTY)
        )
        return ToolResult(
            status=status,
            items=tuple(items),
            truncation=(
                TruncationInfo(
                    reason="PARSE_UNSUPPORTED",
                    limit=len(statements),
                    returned=len(items),
                )
                if unsupported and items
                else None
            ),
            public_summary=f"数据库 Schema Parser 返回 {len(items)} 个字段",
            error_code="PARSE_UNSUPPORTED" if unsupported else None,
        )

    @staticmethod
    def _column_metadata(table_name: str, column: exp.ColumnDef) -> dict:
        nullable = True
        primary_key = False
        default = None
        checks: list[str] = []
        unique = False
        for constraint in column.constraints:
            kind = constraint.kind
            if isinstance(kind, exp.NotNullColumnConstraint):
                nullable = False
            elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
                primary_key = True
                nullable = False
            elif isinstance(kind, exp.DefaultColumnConstraint):
                default = kind.this.sql(dialect="postgres")
            elif isinstance(kind, exp.CheckColumnConstraint):
                checks.append(kind.this.sql(dialect="postgres"))
            elif isinstance(kind, exp.UniqueColumnConstraint):
                unique = True
        return {
            "table": table_name,
            "column": column.name,
            "type": column.kind.sql(dialect="postgres"),
            "nullable": nullable,
            "primary_key": primary_key,
            "unique": unique,
            "default": default,
            "checks": checks,
        }

    @staticmethod
    def _locate(text: str, column: str) -> tuple[int, int, str]:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.strip().startswith(column + " ") or line.strip().startswith(
                f'"{column}" '
            ):
                end = index
                while end + 1 < len(lines) and not lines[end].rstrip().endswith(","):
                    if lines[end + 1].strip() in {")", ");"}:
                        break
                    end += 1
                excerpt = "\n".join(lines[index : end + 1]).strip().rstrip(",")
                return index + 1, end + 1, excerpt
        fallback = lines[0].strip() if lines else ""
        return 1, 1, fallback
