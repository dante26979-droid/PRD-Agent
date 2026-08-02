from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class CoverageStatus(StrEnum):
    MISSING = "MISSING"
    PARTIAL = "PARTIAL"
    COVERED = "COVERED"
    CONFLICTING = "CONFLICTING"


class InvestigationMode(StrEnum):
    INITIAL = "INITIAL"
    REPLAN = "REPLAN"
    SUPPLEMENT = "SUPPLEMENT"


class InvestigationStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    EMPTY = "EMPTY"
    HUMAN_INPUT_REQUIRED = "HUMAN_INPUT_REQUIRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class StopReason(StrEnum):
    COVERAGE_COMPLETE = "COVERAGE_COMPLETE"
    DRAFT_READY = "DRAFT_READY"
    NO_PROGRESS = "NO_PROGRESS"
    MAX_ITERATIONS_REACHED = "MAX_ITERATIONS_REACHED"
    TOOL_BUDGET_EXHAUSTED = "TOOL_BUDGET_EXHAUSTED"
    TOKEN_BUDGET_EXHAUSTED = "TOKEN_BUDGET_EXHAUSTED"
    HUMAN_INPUT_REQUIRED = "HUMAN_INPUT_REQUIRED"
    PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
    REPLAN_EXHAUSTED = "REPLAN_EXHAUSTED"
    MODEL_BUDGET_EXHAUSTED = "MODEL_BUDGET_EXHAUSTED"
    SUPPLEMENT_BUDGET_EXHAUSTED = "SUPPLEMENT_BUDGET_EXHAUSTED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    USER_STOPPED = "USER_STOPPED"
    SOURCE_STALE = "SOURCE_STALE"
    CAPABILITY_FAILED = "CAPABILITY_FAILED"


@dataclass(frozen=True)
class InvestigationBudget:
    max_iterations: int = 3
    max_tool_calls: int = 3
    token_budget: int = 24_000
    no_progress_limit: int = 2
    max_replans: int = 1

    def __post_init__(self) -> None:
        if min(
            self.max_iterations,
            self.max_tool_calls,
            self.token_budget,
            self.no_progress_limit,
        ) < 1:
            raise ValueError("investigation budgets must be positive")
        if self.max_replans < 0:
            raise ValueError("max replans cannot be negative")


@dataclass(frozen=True)
class ProposedAction:
    tool_id: str
    arguments: dict[str, Any]
    purpose: str
    target_coverage: tuple[str, ...]
    tool_schema_version: str = "1"
    strategy: str = "KEYWORD_SEARCH"

    @classmethod
    def from_model_output(
        cls,
        value: Mapping[str, object],
        *,
        active_gap: str,
    ) -> "ProposedAction":
        raw_action = value.get("action")
        action = raw_action if isinstance(raw_action, Mapping) else value
        tool_id = str(action.get("tool_id", "")).strip()
        arguments = action.get("arguments")

        # Compatibility with the previous two-stage planner response.
        if not tool_id:
            repository_queries = value.get("repository_queries")
            if isinstance(repository_queries, (list, tuple)):
                query = next(
                    (
                        item.strip()
                        for item in repository_queries
                        if isinstance(item, str) and item.strip()
                    ),
                    "",
                )
                if query:
                    tool_id = "search_repository"
                    arguments = {"query": query}
            if not tool_id:
                prd_query = value.get("prd_query")
                if isinstance(prd_query, str) and prd_query.strip():
                    tool_id = "search_prd_catalog"
                    arguments = {"query": prd_query.strip()}

        if not tool_id or not isinstance(arguments, Mapping):
            raise ValueError("model response requires one structured action")
        purpose = str(action.get("purpose") or f"调查 {active_gap}").strip()
        raw_targets = action.get("target_coverage", (active_gap,))
        if not isinstance(raw_targets, (list, tuple)):
            raise ValueError("target_coverage must be an array")
        targets = tuple(str(item).strip() for item in raw_targets if str(item).strip())
        if not purpose or not targets:
            raise ValueError("action requires purpose and target coverage")
        return cls(
            tool_id=tool_id,
            tool_schema_version=str(action.get("tool_schema_version", "1")).strip(),
            arguments=dict(arguments),
            purpose=purpose,
            target_coverage=targets,
            strategy=str(action.get("strategy", "KEYWORD_SEARCH")).strip(),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "tool_id": self.tool_id,
            "tool_schema_version": self.tool_schema_version,
            "arguments": self.arguments,
            "purpose": self.purpose,
            "target_coverage": list(self.target_coverage),
            "strategy": self.strategy,
        }


@dataclass(frozen=True)
class ActionRecord:
    round_index: int
    mode: InvestigationMode
    signature: str
    action: ProposedAction
    new_fact_ids: tuple[str, ...]
    progress_before: str | None
    progress_after: str | None


@dataclass(frozen=True)
class ActionSelectionContext:
    mode: InvestigationMode
    active_gap: str
    coverage: dict[str, str]
    action_history: tuple[ActionRecord, ...]
    no_progress_reason: str | None
    remaining_iterations: int
    remaining_tool_calls: int
    remaining_replans: int


@dataclass(frozen=True)
class InvestigationRequest:
    run_id: str
    task_id: str
    mode: InvestigationMode
    need_plan_id: str
    need_context_hash: str
    coverage: dict[str, str]
    source_authorities: tuple[object, ...]
    budget: InvestigationBudget
    prior_knowledge: object | None = None
    prior_action_history: tuple[ActionRecord, ...] = ()
    pending_action: ProposedAction | None = None
    pending_signature: str = ""


@dataclass(frozen=True)
class InvestigationResult:
    status: InvestigationStatus
    stop_reason: StopReason
    coverage: dict[str, str]
    knowledge: object | None
    action_history: tuple[ActionRecord, ...]
    replan_count: int
    no_progress_rounds: int
