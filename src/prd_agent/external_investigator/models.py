from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from prd_agent.evidence.models import SourceEvidence


class CandidateEvidenceLocator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=1_000)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    symbol: str | None = Field(default=None, max_length=300)
    candidate_claim: str | None = Field(default=None, max_length=2_000)


class PublicExternalAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["LIST", "READ", "SEARCH"]
    path: str | None = None
    status: str


class ExternalInvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: str
    repository_id: str
    resolved_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    question: str = Field(min_length=1, max_length=2_000)
    required_coverage: dict[str, str]
    allowed_path_prefixes: tuple[str, ...] = ()
    max_candidates: int = Field(default=20, ge=1, le=100)
    max_total_bytes: int = Field(default=500_000, ge=1, le=10_000_000)
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    access_scope_hash: str | None = None


class ExternalInvestigationResult(BaseModel):
    """Strict provider output. Candidate claims have no evidentiary authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["COMPLETE", "PARTIAL", "EMPTY", "FAILED"]
    candidates: tuple[CandidateEvidenceLocator, ...] = ()
    coverage: dict[str, str] = Field(default_factory=dict)
    unknowns: tuple[str, ...] = ()
    action_trace: tuple[PublicExternalAction, ...] = ()


class ValidatedExternalInvestigation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: str
    status: Literal["COMPLETE", "PARTIAL", "EMPTY", "FAILED"]
    evidence: tuple[SourceEvidence, ...] = ()
    coverage: dict[str, str] = Field(default_factory=dict)
    unknowns: tuple[str, ...] = ()
    action_trace: tuple[PublicExternalAction, ...] = ()
    rejected_candidates: int = 0
