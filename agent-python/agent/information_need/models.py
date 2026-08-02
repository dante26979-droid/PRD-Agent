from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class Requiredness(StrEnum):
    NONE = "NONE"
    OPTIONAL = "OPTIONAL"
    REQUIRED = "REQUIRED"


class NeedKind(StrEnum):
    NEW_BEHAVIOR = "NEW_BEHAVIOR"
    FIELD_OR_FORMAT_CHANGE = "FIELD_OR_FORMAT_CHANGE"
    STATE_OR_RULE_CHANGE = "STATE_OR_RULE_CHANGE"
    PERMISSION_CHANGE = "PERMISSION_CHANGE"
    CODE_LOCATION_ONLY = "CODE_LOCATION_ONLY"
    HISTORICAL_PRD_CONTEXT = "HISTORICAL_PRD_CONTEXT"
    CROSS_MODULE_CHANGE = "CROSS_MODULE_CHANGE"
    UNKNOWN = "UNKNOWN"


class SourceType(StrEnum):
    CODE = "CODE"
    HISTORICAL_PRD = "HISTORICAL_PRD"
    USER_CONTEXT = "USER_CONTEXT"


class NeedRoute(StrEnum):
    SKIP_INVESTIGATION = "SKIP_INVESTIGATION"
    EXECUTE_INVESTIGATION = "EXECUTE_INVESTIGATION"
    PAUSE_FOR_HUMAN = "PAUSE_FOR_HUMAN"


@dataclass(frozen=True)
class NeedPlanningContext:
    run_id: str
    task_id: str
    task_message: str
    task_version: int
    workflow_version: str
    repository_binding_id: str
    repository_revision: str
    historical_prd_available: bool
    remaining_model_attempts: int
    remaining_tool_calls: int
    remaining_iterations: int
    remaining_replans: int
    revision_scope: dict[str, object] | None = None

    @property
    def code_available(self) -> bool:
        return bool(self.repository_binding_id and self.repository_revision)


@dataclass(frozen=True)
class PlannedNeedDraft:
    question: str
    suggested_requiredness: Requiredness
    need_kind: NeedKind
    source_types: tuple[SourceType, ...]
    fallback: str

    def __post_init__(self) -> None:
        if not self.question.strip() or len(self.question) > 1000:
            raise ValueError("information need question is invalid")
        if not self.fallback.strip():
            raise ValueError("information need fallback is required")

    @classmethod
    def from_mapping(cls, value: object) -> "PlannedNeedDraft":
        if not isinstance(value, Mapping):
            raise ValueError("information need draft must be an object")
        expected = {
            "question",
            "suggested_requiredness",
            "need_kind",
            "source_types",
            "fallback",
        }
        if set(value) != expected:
            raise ValueError("information need draft fields are invalid")
        raw_sources = value["source_types"]
        if not isinstance(raw_sources, (list, tuple)):
            raise ValueError("information need source_types must be an array")
        try:
            return cls(
                question=str(value["question"]),
                suggested_requiredness=Requiredness(
                    str(value["suggested_requiredness"])
                ),
                need_kind=NeedKind(str(value["need_kind"])),
                source_types=tuple(SourceType(str(item)) for item in raw_sources),
                fallback=str(value["fallback"]),
            )
        except ValueError as error:
            raise ValueError("information need draft enum is invalid") from error

    def as_dict(self) -> dict[str, object]:
        return {
            "question": self.question,
            "suggested_requiredness": self.suggested_requiredness.value,
            "need_kind": self.need_kind.value,
            "source_types": [item.value for item in self.source_types],
            "fallback": self.fallback,
        }


@dataclass(frozen=True)
class CoverageRequirement:
    key: str
    source_type: SourceType
    description: str
    blocking: bool = True


@dataclass(frozen=True)
class NeedBudgetAllocation:
    max_model_attempts: int
    max_tool_calls: int
    max_iterations: int
    max_replans: int

    def __post_init__(self) -> None:
        if min(
            self.max_model_attempts,
            self.max_tool_calls,
            self.max_iterations,
            self.max_replans,
        ) < 0:
            raise ValueError("information need budget cannot be negative")


@dataclass(frozen=True)
class InformationNeedPlan:
    schema_version: str
    plan_id: str
    context_hash: str
    question: str
    need_kind: NeedKind
    suggested_requiredness: Requiredness
    effective_requiredness: Requiredness
    requiredness_reason_code: str
    source_types: tuple[SourceType, ...]
    required_coverage: tuple[CoverageRequirement, ...]
    route: NeedRoute
    route_reason_code: str
    fallback: str
    budget_allocation: NeedBudgetAllocation
    planner_version: str
    policy_version: str
    assumption_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != "information-need-plan.v1":
            raise ValueError("information need plan schema is incompatible")
        if self.effective_requiredness is Requiredness.NONE:
            if self.source_types or self.required_coverage:
                raise ValueError("NONE information need cannot require investigation")
            if self.route is not NeedRoute.SKIP_INVESTIGATION:
                raise ValueError("NONE information need must skip investigation")
        if self.effective_requiredness is Requiredness.REQUIRED and not any(
            item.blocking for item in self.required_coverage
        ):
            raise ValueError("REQUIRED information need needs blocking coverage")
        if self.route is NeedRoute.EXECUTE_INVESTIGATION and not self.source_types:
            raise ValueError("investigation route needs a source")
        if not self.plan_id.startswith("need-") or not self.context_hash.startswith(
            "sha256:"
        ):
            raise ValueError("information need identity is invalid")


@dataclass(frozen=True)
class PlannedNeedDecision:
    plan: InformationNeedPlan
    route: NeedRoute
    reason_code: str
    replayed: bool = False
    artifact_key: str = ""
    artifact_hash: str = ""
    artifact_request_hash: str = ""
