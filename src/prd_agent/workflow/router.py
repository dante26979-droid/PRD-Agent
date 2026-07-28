from __future__ import annotations

from prd_agent.domain.entities import WorkflowSnapshot
from prd_agent.domain.enums import RunStatus, TaskStatus


def next_public_node(snapshot: WorkflowSnapshot) -> str:
    """Route exclusively from persisted business state, never model reasoning."""

    if snapshot.run.status == RunStatus.FAILED:
        return "END"
    if snapshot.task.status == TaskStatus.CLARIFYING:
        return "WAIT_USER_CLARIFICATION"
    if snapshot.task.status == TaskStatus.OUTLINE_REVIEW:
        return "WAIT_OUTLINE_CONFIRMATION"
    if snapshot.task.status == TaskStatus.GENERATING:
        return "WAIT_UNIT_CONFIRMATION"
    if snapshot.task.status == TaskStatus.FINAL_REVIEW:
        return "FINAL_REVIEW"
    return "LOAD_TASK"

