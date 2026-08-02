from __future__ import annotations

from collections.abc import Mapping

from agent.action_identity import action_signature

from .models import CoverageStatus, InvestigationBudget, ProposedAction, StopReason


SUPPORTED_TOOLS = frozenset({"search_repository", "search_prd_catalog"})


def next_gap(coverage: Mapping[str, str]) -> str | None:
    for status in (
        CoverageStatus.CONFLICTING,
        CoverageStatus.MISSING,
        CoverageStatus.PARTIAL,
    ):
        for key, value in coverage.items():
            if value == status.value:
                return key
    return None


def coverage_complete(coverage: Mapping[str, str]) -> bool:
    return bool(coverage) and all(
        value == CoverageStatus.COVERED.value for value in coverage.values()
    )


def pre_action_stop(state: Mapping[str, object], budget: InvestigationBudget):
    coverage = state.get("coverage", {})
    if isinstance(coverage, Mapping) and coverage_complete(coverage):
        return StopReason.COVERAGE_COMPLETE
    if int(state.get("token_usage", 0)) >= budget.token_budget:
        return StopReason.TOKEN_BUDGET_EXHAUSTED
    if int(state.get("tool_call_count", 0)) >= budget.max_tool_calls:
        return StopReason.TOOL_BUDGET_EXHAUSTED
    if int(state.get("iteration", 0)) >= budget.max_iterations:
        return StopReason.MAX_ITERATIONS_REACHED
    if int(state.get("no_progress_rounds", 0)) >= budget.no_progress_limit:
        return StopReason.NO_PROGRESS
    return None


def validate_action(
    action: ProposedAction,
    *,
    active_gap: str,
    completed_signatures: tuple[str, ...],
) -> str:
    if action.tool_id not in SUPPORTED_TOOLS:
        raise ValueError(f"unsupported Agent tool: {action.tool_id}")
    if action.tool_schema_version != "1":
        raise ValueError("unsupported Agent tool schema version")
    if active_gap not in action.target_coverage:
        raise ValueError("action does not target the active coverage gap")
    query = action.arguments.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError("action query must be non-empty text")
    signature = action_signature(action)
    if signature in completed_signatures:
        raise DuplicateActionError(signature)
    return signature


class DuplicateActionError(ValueError):
    def __init__(self, signature: str) -> None:
        super().__init__("duplicate Agent action")
        self.signature = signature
