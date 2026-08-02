from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from agent.v1 import agent_execution_pb2 as proto

if TYPE_CHECKING:
    from agent.runtime import RuntimeEventSink


@dataclass(frozen=True)
class Lease:
    run_id: str
    lease_id: str
    worker_id: str
    fencing_token: int
    expires_at: str

    def as_proto(self) -> proto.LeaseContext:
        return proto.LeaseContext(
            run_id=self.run_id,
            lease_id=self.lease_id,
            worker_id=self.worker_id,
            fencing_token=self.fencing_token,
            expires_at=self.expires_at,
        )


@dataclass(frozen=True)
class RunContext:
    run_id: str
    tenant_id: str
    owner_id: str
    task_id: str
    task_message: str
    workflow_version: str
    checkpoint: bytes
    checkpoint_sequence: int = 0
    task_version: int = 1
    dispatch_id: str = ""
    lease: Lease | None = None
    repository_binding_id: str = ""
    repository_revision: str = ""
    resume_evidence: tuple[proto.EvidenceItem, ...] = ()
    resume_artifacts: tuple[proto.RunArtifact, ...] = ()
    revision_scope: proto.RevisionScope | None = None
    resume_draft: proto.SubmittedDraftReceipt | None = None
    base_draft: proto.SubmittedDraftReceipt | None = None
    submitted_draft: proto.SubmittedDraftReceipt | None = None
    resume_summary: proto.ResumeStateSummary | None = None
    execution_ledger_version: str = ""
    run_budget: proto.RunBudget | None = None
    consumed_budget: proto.ConsumedBudget | None = None
    ledger_entries: tuple[proto.RunLedgerEntry, ...] = ()
    allowed_source_authorities: tuple[proto.SourceAuthority, ...] = ()
    run_purpose: proto.RunPurpose = proto.RUN_PURPOSE_UNSPECIFIED
    unit_scope: proto.UnitScope | None = None
    evaluation_mode: str = ""
    authoritative_workflow_version: str = ""
    shadow_workflow_version: str = ""
    candidate_policy_version: str = ""
    assignment_hash: str = ""
    plan_model_attempt: Callable[[proto.RecordModelAttemptRequest], None] | None = None
    event_sink: RuntimeEventSink | None = None
