from __future__ import annotations

import uuid

from prd_agent.tools.models import ToolAction, ToolResult

from .models import (
    DeterministicFact,
    FactType,
    SourceEvidence,
    VerificationStatus,
)


class DeterministicFactBuilder:
    _parser_tools = frozenset({"parse_openapi", "parse_database_schema"})

    def build(
        self,
        action: ToolAction,
        result: ToolResult,
        evidence: tuple[SourceEvidence, ...],
        *,
        task_id: str | None = None,
    ) -> tuple[DeterministicFact, ...]:
        if action.tool_id not in self._parser_tools:
            return ()
        if len(result.items) != len(evidence):
            return ()
        facts = []
        for item, source in zip(result.items, evidence, strict=True):
            if action.tool_id == "parse_openapi" and item.kind == "openapi_field":
                operation = str(item.metadata["operation"])
                field = str(item.metadata["field"])
                subject = f"{operation}.request.{field}"
                predicate = "api_field_contract"
                value = {
                    key: item.metadata[key]
                    for key in sorted(item.metadata)
                    if key not in {"operation", "field"}
                }
            elif (
                action.tool_id == "parse_database_schema"
                and item.kind == "database_column"
            ):
                table = str(item.metadata["table"])
                column = str(item.metadata["column"])
                subject = f"{table}.{column}"
                predicate = "database_column_contract"
                value = {
                    key: item.metadata[key]
                    for key in sorted(item.metadata)
                    if key not in {"table", "column"}
                }
            else:
                continue
            facts.append(
                DeterministicFact(
                    fact_id=f"fact-{uuid.uuid4().hex}",
                    tool_call_id=source.tool_call_id,
                    task_id=task_id,
                    subject=subject,
                    predicate=predicate,
                    value_json=value,
                    fact_type=FactType.CODE_VERIFIED,
                    confidence="HIGH",
                    verification_status=VerificationStatus.SUPPORTED,
                    extractor_id=action.tool_id,
                    extractor_version=action.tool_schema_version,
                    evidence_ids=(source.evidence_id,),
                )
            )
        return tuple(facts)
