from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Requiredness(StrEnum):
    NONE = "NONE"
    OPTIONAL = "OPTIONAL"
    REQUIRED = "REQUIRED"


class NeedStatus(StrEnum):
    PLANNED = "PLANNED"
    SKIPPED = "SKIPPED"
    INVESTIGATING = "INVESTIGATING"
    SATISFIED = "SATISFIED"
    UNSATISFIED = "UNSATISFIED"


class CoverageStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    MISSING = "MISSING"
    PARTIAL = "PARTIAL"
    COVERED = "COVERED"
    CONFLICTING = "CONFLICTING"


class InvestigationStatus(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    EMPTY = "EMPTY"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    HUMAN_INPUT_REQUIRED = "HUMAN_INPUT_REQUIRED"


class StopReason(StrEnum):
    COVERAGE_COMPLETE = "COVERAGE_COMPLETE"
    NO_PROGRESS = "NO_PROGRESS"
    MAX_ITERATIONS_REACHED = "MAX_ITERATIONS_REACHED"
    TOOL_BUDGET_EXHAUSTED = "TOOL_BUDGET_EXHAUSTED"
    TOKEN_BUDGET_EXHAUSTED = "TOKEN_BUDGET_EXHAUSTED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    HUMAN_INPUT_REQUIRED = "HUMAN_INPUT_REQUIRED"
    USER_STOPPED = "USER_STOPPED"
    UNRECOVERABLE_ERROR = "UNRECOVERABLE_ERROR"


class InformationNeed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    information_need_id: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=1000)
    requiredness: Requiredness
    source_types: tuple[str, ...] = ()
    required_coverage: tuple[str, ...] = ()
    trigger_stage: str = Field(min_length=1)
    fallback: str = Field(min_length=1)
    status: NeedStatus = NeedStatus.PLANNED
    planner_version: str = Field(min_length=1)
    context_hash: str = Field(min_length=1)
    task_id: str | None = None
    run_id: str | None = None
    unit_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_required_coverage(self) -> "InformationNeed":
        if self.requiredness == Requiredness.NONE and self.required_coverage:
            raise ValueError("NONE information need cannot require coverage")
        if self.requiredness == Requiredness.REQUIRED and not self.required_coverage:
            raise ValueError("REQUIRED information need requires coverage")
        if self.requiredness != Requiredness.NONE and not self.source_types:
            raise ValueError("investigated information need requires source_types")
        return self


class CoverageItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    description: str = Field(min_length=1)
    status: CoverageStatus = CoverageStatus.MISSING
    evidence_ids: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()
    unknown_ids: tuple[str, ...] = ()
    conflict_ids: tuple[str, ...] = ()
    updated_at_iteration: int | None = Field(default=None, ge=1)


class InvestigationBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_iterations: int = Field(default=5, ge=1, le=50)
    max_tool_calls: int = Field(default=12, ge=1, le=100)
    max_replans: int = Field(default=1, ge=0, le=5)
    no_progress_limit: int = Field(default=2, ge=1, le=10)
    same_action_limit: Literal[1] = 1
    token_budget: int = Field(default=12000, ge=1)


class Investigation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: str = Field(min_length=1)
    information_need_id: str = Field(min_length=1)
    repository_id: str = Field(min_length=1)
    resolved_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    status: InvestigationStatus = InvestigationStatus.PLANNED
    coverage: dict[str, CoverageItem]
    budget: InvestigationBudget = Field(default_factory=InvestigationBudget)
    iteration_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    replan_count: int = Field(default=0, ge=0)
    no_progress_rounds: int = Field(default=0, ge=0)
    token_usage: int = Field(default=0, ge=0)
    completed_action_signatures: frozenset[str] = frozenset()
    evidence_ids: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()
    unknown_ids: tuple[str, ...] = ()
    conflict_ids: tuple[str, ...] = ()
    stop_reason: StopReason | None = None
    policy_version: str = "investigation.v1"
    version: int = Field(default=1, ge=1)
    task_id: str | None = None
    run_id: str | None = None
    unit_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str = Field(min_length=1)
    tool_schema_version: str = Field(default="1", min_length=1)
    arguments: dict[str, Any]
    purpose: str = Field(min_length=1, max_length=500)
    target_coverage: tuple[str, ...] = ()


class InvestigationStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    investigation_id: str
    sequence: int = Field(ge=1)
    iteration: int = Field(ge=0)
    step_type: str
    status: str
    public_summary: str
    input_hash: str
    output: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class InvestigationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: str
    status: InvestigationStatus
    stop_reason: StopReason
    coverage: dict[str, CoverageItem]
    evidence_ids: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()
    unknown_ids: tuple[str, ...] = ()
    conflict_ids: tuple[str, ...] = ()
    public_summary: str
    risk_summary: tuple[str, ...] = ()
