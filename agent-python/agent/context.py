from __future__ import annotations

from dataclasses import dataclass

from agent.v1 import agent_execution_pb2 as proto


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
