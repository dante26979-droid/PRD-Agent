from __future__ import annotations

from dataclasses import dataclass

from agent.v1 import agent_execution_pb2 as proto


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
