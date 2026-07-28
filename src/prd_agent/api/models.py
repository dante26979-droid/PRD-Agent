from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from prd_agent.export.models import ExportMode


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskSummaryView(StrictModel):
    task_id: str
    title: str
    task_status: str
    run_status: str | None = None
    display_status: str
    version: int
    updated_at: datetime
    current_unit_sequence: int | None = None
    attention_required: bool


class TaskListResponse(StrictModel):
    items: list[TaskSummaryView]
    next_cursor: str | None = None


class RunView(StrictModel):
    run_id: str
    status: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: str | None = None


class MessageView(StrictModel):
    message_id: str
    role: Literal["user", "agent", "system"]
    content: str
    created_at: datetime


class OutlineNodeView(StrictModel):
    node_id: str
    sequence: int
    title: str
    purpose: str
    complexity: str
    parent_id: str | None = None
    level: int


class UnitView(StrictModel):
    unit_id: str
    sequence: int
    title: str
    status: str
    node_ids: list[str]
    depends_on_unit_ids: list[str]
    content: str | None = None
    content_hash: str | None = None


class OutlineView(StrictModel):
    outline_id: str
    version: int
    title: str
    status: str
    nodes: list[OutlineNodeView]
    units: list[UnitView]


class GroundingView(StrictModel):
    grounding_run_id: str
    unit_id: str
    status: str
    confirmable: bool
    issues: list[str]
    reference_count: int


class CoverageItemView(StrictModel):
    key: str
    description: str
    status: str
    evidence_ids: list[str]
    fact_ids: list[str]
    unknown_ids: list[str]
    conflict_ids: list[str]


class InvestigationStepView(StrictModel):
    sequence: int
    iteration: int
    step_type: str
    status: str
    public_summary: str


class ToolCallView(StrictModel):
    tool_call_id: str
    tool_id: str
    status: str
    purpose: str
    public_summary: str
    error_code: str | None = None
    started_at: datetime
    ended_at: datetime | None = None


class EvidenceView(StrictModel):
    evidence_id: str
    source_kind: str
    source_id: str
    source_version: str
    locator: dict[str, Any]
    excerpt: str
    content_hash: str
    extraction_method: str
    redaction_applied: bool


class FactView(StrictModel):
    fact_id: str
    subject: str
    predicate: str
    value: Any
    fact_scope: str
    fact_type: str
    verification_status: str
    evidence_ids: list[str]


class UnknownView(StrictModel):
    unknown_id: str
    statement: str
    reason: str
    severity: str
    resolution_type: str


class ConflictView(StrictModel):
    conflict_id: str
    subject: str
    description: str
    fact_ids: list[str]
    status: str


class InvestigationView(StrictModel):
    investigation_id: str
    information_need_id: str
    unit_id: str | None = None
    question: str
    requiredness: str
    source_types: list[str]
    status: str
    stop_reason: str | None = None
    coverage: list[CoverageItemView]
    steps: list[InvestigationStepView]
    tool_calls: list[ToolCallView]
    evidence: list[EvidenceView]
    facts: list[FactView]
    unknowns: list[UnknownView]
    conflicts: list[ConflictView]


class QualityIssueView(StrictModel):
    issue_id: str
    issue_type: str
    severity: str
    description: str
    suggested_resolution: str
    affected_unit_ids: list[str]


class QualityView(StrictModel):
    quality_run_id: str
    scope: str
    scope_id: str
    confirmable: bool
    issues: list[QualityIssueView]


class DocumentView(StrictModel):
    document_id: str
    version: int
    status: str
    content_hash: str
    markdown: str
    created_at: datetime


class AvailableActionView(StrictModel):
    type: str
    label: str
    expected_task_version: int
    target_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    destructive: bool = False


class TaskDetailResponse(StrictModel):
    task: TaskSummaryView
    run: RunView
    brief: dict[str, Any] | None = None
    messages: list[MessageView]
    outline: OutlineView | None = None
    investigations: list[InvestigationView]
    grounding: list[GroundingView]
    quality: list[QualityView]
    document: DocumentView | None = None
    available_actions: list[AvailableActionView]
    latest_event_sequence: int


class StartTaskRequest(StrictModel):
    message: str = Field(min_length=1, max_length=20_000)


class ReplyRequest(StrictModel):
    message: str = Field(min_length=1, max_length=20_000)
    expected_task_version: int = Field(ge=1)


class ConfirmOutlineRequest(StrictModel):
    outline_version: int = Field(ge=1)
    expected_task_version: int = Field(ge=1)


class ConfirmUnitRequest(StrictModel):
    expected_task_version: int = Field(ge=1)


class ApproveRevisionRequest(StrictModel):
    document_id: str = Field(min_length=1)
    issue_ids: tuple[str, ...] = Field(min_length=1)
    unit_ids: tuple[str, ...] = Field(min_length=1)
    expected_task_version: int = Field(ge=1)


class FinalizeRequest(StrictModel):
    document_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    expected_task_version: int = Field(ge=1)


class ReopenRequest(StrictModel):
    unit_ids: tuple[str, ...] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2_000)
    expected_task_version: int = Field(ge=1)


class RetryRunRequest(StrictModel):
    expected_task_version: int = Field(ge=1)


class ExportPreviewRequest(StrictModel):
    mode: ExportMode
    expected_task_version: int = Field(ge=1)


class ExecuteExportRequest(StrictModel):
    intent_id: str = Field(min_length=1)
    confirmation_token: str = Field(min_length=1)
    mode: ExportMode
    expected_task_version: int = Field(ge=1)


class ExportPreviewView(StrictModel):
    intent_id: str
    confirmation_token: str
    mode: str
    title: str
    task_version: int
    document_version: int
    content_hash: str
    unresolved_items: list[str]
    expires_at: datetime
    bound_title: str | None = None
    bound_safe_url: str | None = None


class ExportRunView(StrictModel):
    export_run_id: str
    intent_id: str
    mode: str
    task_version: int
    document_version: int
    content_hash: str
    status: str
    attempt_count: int
    binding_id: str | None = None
    safe_url: str | None = None
    display_title: str | None = None
    error_code: str | None = None
    retryable: bool
    created_at: datetime
    completed_at: datetime | None = None


class ExportListResponse(StrictModel):
    items: list[ExportRunView]


class RepositoryBindingView(StrictModel):
    repository_id: str
    provider: str
    display_name: str
    default_revision: str
    allowed_prefix: str


class RepositoryBindingListResponse(StrictModel):
    items: list[RepositoryBindingView]


class PublicEvent(StrictModel):
    event_id: str
    task_id: str
    sequence: int
    event: str
    occurred_at: datetime
    payload: dict[str, Any]


class ErrorResponse(StrictModel):
    type: str = "about:blank"
    title: str
    status: int
    error_code: str
    message: str
    retryable: bool = False
    correlation_id: str
