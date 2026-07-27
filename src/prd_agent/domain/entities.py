from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .enums import (
    Complexity,
    DocumentStatus,
    OutlineStatus,
    RunStatus,
    SectionStatus,
    TaskStatus,
    UnitStatus,
)
from .errors import ModelOutputError


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _strings(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ModelOutputError(f"{field_name} must be an array")
    items = tuple(str(item).strip() for item in value)
    if any(not item for item in items):
        raise ModelOutputError(f"{field_name} cannot contain empty values")
    return items


@dataclass
class Task:
    task_id: str
    title: str | None = None
    status: TaskStatus = TaskStatus.DRAFT
    version: int = 1
    current_outline_version: int | None = None
    current_unit_sequence: int | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    owner_id: str = "local-user"


@dataclass(frozen=True)
class RequirementBrief:
    problem: str
    target_users: tuple[str, ...] = ()
    scenarios: tuple[str, ...] = ()
    goals: tuple[str, ...] = ()
    scope_in: tuple[str, ...] = ()
    scope_out: tuple[str, ...] = ()
    product_rules: tuple[str, ...] = ()
    success_metrics: tuple[str, ...] = ()
    current_state_fact_ids: tuple[str, ...] = ()
    target_decisions: tuple[str, ...] = ()
    authorized_assumptions: tuple[str, ...] = ()
    agent_suggestions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    unknown_item_ids: tuple[str, ...] = ()
    source_conflict_ids: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RequirementBrief":
        if not isinstance(value, Mapping):
            raise ModelOutputError("requirement brief must be an object")
        problem = str(value.get("problem", "")).strip()
        if not problem:
            raise ModelOutputError("requirement brief requires problem")
        protected = (
            "current_state_fact_ids",
            "unknown_item_ids",
            "source_conflict_ids",
        )
        if any(value.get(name) for name in protected):
            raise ModelOutputError("investigation-backed fields must remain empty in step 2")
        names = (
            "target_users",
            "scenarios",
            "goals",
            "scope_in",
            "scope_out",
            "product_rules",
            "success_metrics",
            "current_state_fact_ids",
            "target_decisions",
            "authorized_assumptions",
            "agent_suggestions",
            "open_questions",
            "unknown_item_ids",
            "source_conflict_ids",
        )
        return cls(problem=problem, **{name: _strings(value.get(name), name) for name in names})

    def as_dict(self) -> dict[str, Any]:
        return {
            name: list(value) if isinstance(value, tuple) else value
            for name, value in vars(self).items()
        }

    @property
    def needs_clarification(self) -> bool:
        required_context = self.target_users and self.scenarios and self.goals and self.scope_in
        return bool(self.open_questions) or not bool(required_context)


@dataclass(frozen=True)
class RequirementBriefVersion:
    task_id: str
    version: int
    brief: RequirementBrief
    confirmed: bool = True
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class OutlineNode:
    node_id: str
    sequence: int
    title: str
    purpose: str
    complexity: Complexity
    required_information: tuple[str, ...] = ()
    parent_id: str | None = None
    level: int = 1
    stable_key: str | None = None


@dataclass
class ConfirmationUnit:
    unit_id: str
    outline_id: str
    sequence: int
    title: str
    status: UnitStatus = UnitStatus.PENDING
    content: str | None = None
    version: int = 1
    node_ids: tuple[str, ...] = ()
    depends_on_unit_ids: tuple[str, ...] = ()
    content_hash: str | None = None
    grounding_run_id: str | None = None
    quality_run_id: str | None = None
    section_drafts: tuple[Mapping[str, str], ...] = ()


@dataclass
class OutlineVersion:
    outline_id: str
    task_id: str
    version: int
    title: str
    status: OutlineStatus
    nodes: tuple[OutlineNode, ...]
    confirmation_units: tuple[ConfirmationUnit, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    confirmed_at: datetime | None = None


@dataclass
class AgentRun:
    run_id: str
    task_id: str
    thread_id: str
    graph_name: str = "prd_workflow"
    graph_version: str = "m0.step6.v1"
    status: RunStatus = RunStatus.QUEUED
    input_hash: str = ""
    idempotency_key: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: str | None = None


@dataclass(frozen=True)
class TaskMessage:
    message_id: str
    task_id: str
    actor_id: str
    content: str
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class PrdSectionVersion:
    section_id: str
    unit_id: str
    version: int
    title: str
    content: str
    created_at: datetime = field(default_factory=utc_now)
    node_id: str | None = None
    status: SectionStatus = SectionStatus.CONFIRMED
    content_hash: str = ""
    source_run_id: str | None = None
    grounding_run_id: str | None = None
    quality_run_id: str | None = None
    supersedes_section_id: str | None = None
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None


@dataclass(frozen=True)
class PrdDocumentVersion:
    document_id: str
    task_id: str
    version: int
    markdown: str
    created_at: datetime = field(default_factory=utc_now)
    content_hash: str = ""
    status: DocumentStatus = DocumentStatus.FINAL_REVIEW
    section_ids: tuple[str, ...] = ()
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None


@dataclass(frozen=True)
class DomainEvent:
    event_id: str
    task_id: str
    sequence: int
    event_type: str
    payload: Mapping[str, Any]
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class IdempotencyRecord:
    actor_id: str
    idempotency_key: str
    input_hash: str
    task_id: str
    run_id: str
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class WorkflowSnapshot:
    task: Task
    run: AgentRun
    briefs: tuple[RequirementBriefVersion, ...]
    outlines: tuple[OutlineVersion, ...]
    sections: tuple[PrdSectionVersion, ...]
    documents: tuple[PrdDocumentVersion, ...]
    messages: tuple[TaskMessage, ...] = ()
    quality_results: tuple[Any, ...] = ()
    grounding_results: tuple[Any, ...] = ()

    @property
    def current_brief(self) -> RequirementBriefVersion | None:
        return self.briefs[-1] if self.briefs else None

    @property
    def current_outline(self) -> OutlineVersion | None:
        active = [item for item in self.outlines if item.status != OutlineStatus.SUPERSEDED]
        return active[-1] if active else None

    @property
    def markdown(self) -> str | None:
        return self.documents[-1].markdown if self.documents else None
