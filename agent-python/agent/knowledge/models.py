from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Mapping

from agent.v1 import agent_execution_pb2 as proto

if TYPE_CHECKING:
    from agent.investigation.models import ProposedAction


class FactScope(StrEnum):
    CURRENT_STATE = "CURRENT_STATE"
    HISTORICAL_CONTEXT = "HISTORICAL_CONTEXT"
    TARGET_DECISION = "TARGET_DECISION"
    ASSUMPTION = "ASSUMPTION"


class FactType(StrEnum):
    USER_CONFIRMED = "USER_CONFIRMED"
    CODE_VERIFIED = "CODE_VERIFIED"
    DOCUMENT_SUPPORTED = "DOCUMENT_SUPPORTED"
    INFERRED = "INFERRED"


class VerificationStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICTING = "CONFLICTING"


@dataclass(frozen=True)
class SourceAuthority:
    source_kind: str
    binding_id: str
    source_id: str
    source_version: str
    access_scope_hash: str = ""


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    source_kind: str
    binding_id: str
    source_id: str
    source_version: str
    access_scope_hash: str
    locator: str
    excerpt_hash: str
    excerpt: str
    validation_status: str
    action_signature: str


@dataclass(frozen=True)
class VerifiedFact:
    fact_id: str
    subject: str
    predicate: str
    value: object
    fact_scope: FactScope
    fact_type: FactType
    verification_status: VerificationStatus
    evidence_ids: tuple[str, ...]
    coverage_keys: tuple[str, ...]
    extractor_id: str
    extractor_version: str


@dataclass(frozen=True)
class Unknown:
    unknown_id: str
    coverage_key: str
    question: str
    reason_code: str
    action_signature: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceConflict:
    conflict_id: str
    subject: str
    predicate: str
    fact_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    status: str = "OPEN"


@dataclass(frozen=True)
class CoverageUpdate:
    coverage_key: str
    before: str
    after: str
    supported_fact_ids: tuple[str, ...] = ()
    unknown_ids: tuple[str, ...] = ()
    conflict_ids: tuple[str, ...] = ()
    reason_code: str = ""


@dataclass(frozen=True)
class KnowledgeBundle:
    schema_version: str
    bundle_id: str
    run_id: str
    task_id: str
    need_plan_id: str
    need_context_hash: str
    evidence: tuple[EvidenceRecord, ...]
    facts: tuple[VerifiedFact, ...]
    unknowns: tuple[Unknown, ...]
    conflicts: tuple[SourceConflict, ...]
    coverage_updates: tuple[CoverageUpdate, ...]
    knowledge_fingerprint: str
    progress_fingerprint: str
    builder_version: str = "knowledge-builder.v1"


@dataclass(frozen=True)
class KnowledgeBuildRequest:
    run_id: str
    task_id: str
    need_plan_id: str
    need_context_hash: str
    action: ProposedAction
    evidence_items: tuple[proto.EvidenceItem, ...]
    source_authorities: tuple[SourceAuthority, ...]
    coverage: Mapping[str, str]
    prior_bundle: KnowledgeBundle | None = None
    outcome_kind: str = "HIT"


@dataclass(frozen=True)
class KnowledgeBuildResult:
    bundle: KnowledgeBundle
    coverage: dict[str, str]
    progressed: bool
