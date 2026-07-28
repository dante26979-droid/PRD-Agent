from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from prd_agent.evidence.models import DeterministicFact, SourceConflict, SourceEvidence
from prd_agent.sources.models import SourceBinding, SourceKind


class GroundingVerdict(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICTING = "CONFLICTING"
    STALE_SOURCE = "STALE_SOURCE"
    INVALID_SOURCE = "INVALID_SOURCE"


class ClaimKind(StrEnum):
    CURRENT_STATE = "CURRENT_STATE"
    HISTORICAL_CONTEXT = "HISTORICAL_CONTEXT"
    TARGET_DECISION = "TARGET_DECISION"
    ASSUMPTION = "ASSUMPTION"
    RECOMMENDATION = "RECOMMENDATION"
    UNKNOWN = "UNKNOWN"
    RISK = "RISK"


class ClaimCriticality(StrEnum):
    CRITICAL = "CRITICAL"
    MATERIAL = "MATERIAL"
    INFORMATIONAL = "INFORMATIONAL"


class GroundingAction(StrEnum):
    PASS = "PASS"
    RETRY_INVESTIGATION = "RETRY_INVESTIGATION"
    DELETE_CLAIM = "DELETE_CLAIM"
    DOWNGRADE_TO_ASSUMPTION = "DOWNGRADE_TO_ASSUMPTION"
    CONVERT_TO_UNKNOWN = "CONVERT_TO_UNKNOWN"
    CONVERT_TO_RISK = "CONVERT_TO_RISK"
    HUMAN_CONFIRMATION_REQUIRED = "HUMAN_CONFIRMATION_REQUIRED"


class GroundingStatus(StrEnum):
    PASSED = "PASSED"
    DEGRADED = "DEGRADED"
    HUMAN_INPUT_REQUIRED = "HUMAN_INPUT_REQUIRED"
    FAILED = "FAILED"


class PrdClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: ClaimKind
    criticality: ClaimCriticality = ClaimCriticality.MATERIAL
    fact_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    assumption_ids: tuple[str, ...] = ()


class GroundingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    grounding_run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    unit_id: str = Field(min_length=1)
    repository_id: str | None = Field(default=None, min_length=1)
    resolved_commit_sha: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{40}$"
    )
    allowed_source_bindings: tuple[SourceBinding, ...] = ()
    content: str = Field(min_length=1)
    claims: tuple[PrdClaim, ...] = ()
    facts: tuple[DeterministicFact, ...] = ()
    evidence: tuple[SourceEvidence, ...] = ()
    conflicts: tuple[SourceConflict, ...] = ()
    retry_count: int = Field(default=0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_source_scope(self) -> "GroundingRequest":
        if bool(self.repository_id) != bool(self.resolved_commit_sha):
            raise ValueError("repository_id and resolved_commit_sha must be provided together")
        if not self.repository_id and not self.allowed_source_bindings:
            raise ValueError("at least one allowed source binding is required")
        return self


class GroundingSupplement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    facts: tuple[DeterministicFact, ...] = ()
    evidence: tuple[SourceEvidence, ...] = ()
    conflicts: tuple[SourceConflict, ...] = ()


class FactGroundingAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str
    evidence_ids: tuple[str, ...]
    verdict: GroundingVerdict
    reason_code: str


class ClaimGroundingAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str
    kind: ClaimKind
    verdict: GroundingVerdict
    valid_fact_ids: tuple[str, ...] = ()
    invalid_fact_ids: tuple[str, ...] = ()
    action: GroundingAction
    reason_code: str


class GroundingReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_ids: tuple[str, ...]
    fact_id: str
    evidence_id: str
    source_kind: SourceKind = SourceKind.CODE_REPOSITORY
    source_id: str
    source_version: str
    locator: dict = Field(default_factory=dict)
    repository_id: str | None = None
    resolved_commit_sha: str | None = None
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    symbol: str | None = None


class GroundingResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grounding_run_id: str
    task_id: str
    unit_id: str
    status: GroundingStatus
    confirmable: bool
    fact_assessments: tuple[FactGroundingAssessment, ...] = ()
    claim_assessments: tuple[ClaimGroundingAssessment, ...] = ()
    used_fact_ids: tuple[str, ...] = ()
    references: tuple[GroundingReference, ...] = ()
    retry_count: int = Field(default=0, ge=0, le=1)
    issues: tuple[str, ...] = ()
