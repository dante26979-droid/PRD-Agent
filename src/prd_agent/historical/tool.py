from __future__ import annotations

from prd_agent.sources.models import ReadAction, SourceKind

from .models import HistoricalPrdSearchArguments

HISTORICAL_PRD_SEARCH_TOOL_ID = "historical_prd_search"
HISTORICAL_PRD_SEARCH_SCHEMA_VERSION = "1"


class HistoricalPrdSearchTool:
    """Typed tool boundary; retrieval hits remain evidence candidates, not facts."""

    def __init__(self, retriever) -> None:
        self.retriever = retriever

    def execute(
        self,
        action: ReadAction,
        *,
        investigation_id: str,
        owner_id: str,
        project_id: str,
        access_labels: tuple[str, ...] = (),
        allow_keyword_fallback: bool = False,
    ):
        if (
            action.tool_id != HISTORICAL_PRD_SEARCH_TOOL_ID
            or action.tool_schema_version != HISTORICAL_PRD_SEARCH_SCHEMA_VERSION
        ):
            raise ValueError("unsupported historical PRD tool contract")
        if (
            action.source_binding.source_kind
            != SourceKind.HISTORICAL_PRD_CORPUS
        ):
            raise ValueError("historical PRD search requires a corpus binding")
        arguments = HistoricalPrdSearchArguments.model_validate(action.arguments)
        return self.retriever.search(
            investigation_id=investigation_id,
            binding=action.source_binding,
            arguments=arguments,
            owner_id=owner_id,
            project_id=project_id,
            access_labels=access_labels,
            allow_keyword_fallback=allow_keyword_fallback,
        )
