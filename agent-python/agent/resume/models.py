from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.context import RunContext
from agent.graph.snapshot import LoopCheckpointStatus
from agent.result import SubmissionDisposition


@dataclass(frozen=True)
class ValidatedRunState:
    context: RunContext
    status: LoopCheckpointStatus
    state: dict[str, Any] | None
    submission_disposition: SubmissionDisposition = SubmissionDisposition.NOT_READY


@dataclass(frozen=True)
class SnapshotIdentity:
    run_id: str
    task_id: str
    workflow_version: str
    base_task_version: int
    repository_binding_id: str
    repository_revision: str
