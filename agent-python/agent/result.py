from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agent.v1 import agent_execution_pb2 as proto


class SubmissionDisposition(StrEnum):
    NOT_READY = "NOT_READY"
    SUBMIT_REQUIRED = "SUBMIT_REQUIRED"
    TERMINAL_ACK_ONLY = "TERMINAL_ACK_ONLY"


@dataclass(frozen=True)
class AgentResult:
    attempt: proto.RecordModelAttemptRequest | None = None
    additional_attempts: tuple[proto.RecordModelAttemptRequest, ...] = ()
    evidence: tuple[proto.EvidenceItem, ...] = ()
    checkpoint_sequence: int | None = None
    checkpoint: bytes = b""
    draft_key: str | None = None
    expected_task_version: int | None = None
    draft_patch: bytes = b""
    run_output: proto.RunOutput | None = None
    submission_disposition: SubmissionDisposition = SubmissionDisposition.SUBMIT_REQUIRED
