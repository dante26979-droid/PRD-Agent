from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class QualityScope(StrEnum):
    UNIT = "UNIT"
    DOCUMENT = "DOCUMENT"


class QualitySeverity(StrEnum):
    BLOCKER = "BLOCKER"
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class QualityIssueType(StrEnum):
    OUTLINE_COVERAGE = "OUTLINE_COVERAGE"
    EMPTY_OR_DUPLICATE_CONTENT = "EMPTY_OR_DUPLICATE_CONTENT"
    TERMINOLOGY_INCONSISTENCY = "TERMINOLOGY_INCONSISTENCY"
    RULE_CONFLICT = "RULE_CONFLICT"
    STATE_FLOW_GAP = "STATE_FLOW_GAP"
    ROLE_PERMISSION_CONFLICT = "ROLE_PERMISSION_CONFLICT"
    EXCEPTION_GAP = "EXCEPTION_GAP"
    BOUNDARY_GAP = "BOUNDARY_GAP"
    ACCEPTANCE_NOT_EXECUTABLE = "ACCEPTANCE_NOT_EXECUTABLE"
    ASSUMPTION_NOT_LABELED = "ASSUMPTION_NOT_LABELED"
    OPEN_ITEM_UNRESOLVED = "OPEN_ITEM_UNRESOLVED"
    SOURCE_LINK_INCOMPLETE = "SOURCE_LINK_INCOMPLETE"


class QualityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_id: str
    issue_type: QualityIssueType
    severity: QualitySeverity
    affected_node_ids: tuple[str, ...] = ()
    affected_unit_ids: tuple[str, ...] = ()
    affected_section_ids: tuple[str, ...] = ()
    description: str
    suggested_resolution: str


class QualityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quality_run_id: str
    task_id: str
    scope: QualityScope
    scope_id: str
    confirmable: bool
    issues: tuple[QualityIssue, ...] = ()
