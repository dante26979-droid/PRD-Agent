from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from agent.v1 import agent_execution_pb2 as proto


class RuntimeEventSink(Protocol):
    """Synchronous durability boundary used by Agent graph nodes."""

    def model_attempt(self, attempt: proto.RecordModelAttemptRequest) -> None: ...

    def evidence(self, items: tuple[proto.EvidenceItem, ...]) -> None: ...

    def artifact(self, item: proto.RunArtifact) -> None: ...

    def checkpoint(self, sequence: int, payload: bytes) -> None: ...


@dataclass
class BufferedRuntimeEventSink:
    """In-process sink used by direct calls and graph unit tests."""

    model_attempts: list[proto.RecordModelAttemptRequest] = field(default_factory=list)
    evidence_items: list[proto.EvidenceItem] = field(default_factory=list)
    artifacts: list[proto.RunArtifact] = field(default_factory=list)
    checkpoints: list[tuple[int, bytes]] = field(default_factory=list)

    def model_attempt(self, attempt: proto.RecordModelAttemptRequest) -> None:
        self.model_attempts.append(attempt)

    def evidence(self, items: tuple[proto.EvidenceItem, ...]) -> None:
        self.evidence_items.extend(items)

    def artifact(self, item: proto.RunArtifact) -> None:
        self.artifacts.append(item)

    def checkpoint(self, sequence: int, payload: bytes) -> None:
        self.checkpoints.append((sequence, payload))
