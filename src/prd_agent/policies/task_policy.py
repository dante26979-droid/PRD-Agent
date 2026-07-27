from __future__ import annotations

from prd_agent.domain.enums import TaskStatus
from prd_agent.domain.errors import InvalidTransition, VersionConflict


class TaskTransitionPolicy:
    _allowed = {
        TaskStatus.DRAFT: {TaskStatus.CLARIFYING, TaskStatus.DELETING},
        TaskStatus.CLARIFYING: {
            TaskStatus.OUTLINE_REVIEW,
            TaskStatus.FAILED,
            TaskStatus.STOPPED,
            TaskStatus.DELETING,
        },
        TaskStatus.OUTLINE_REVIEW: {
            TaskStatus.CLARIFYING,
            TaskStatus.GENERATING,
            TaskStatus.DELETING,
        },
        TaskStatus.GENERATING: {
            TaskStatus.OUTLINE_REVIEW,
            TaskStatus.FINAL_REVIEW,
            TaskStatus.FAILED,
            TaskStatus.STOPPED,
            TaskStatus.DELETING,
        },
        TaskStatus.FINAL_REVIEW: {
            TaskStatus.GENERATING,
            TaskStatus.COMPLETED,
            TaskStatus.DELETING,
        },
        TaskStatus.COMPLETED: {TaskStatus.GENERATING, TaskStatus.DELETING},
        TaskStatus.FAILED: {
            TaskStatus.CLARIFYING,
            TaskStatus.OUTLINE_REVIEW,
            TaskStatus.GENERATING,
            TaskStatus.DELETING,
        },
        TaskStatus.STOPPED: {
            TaskStatus.CLARIFYING,
            TaskStatus.OUTLINE_REVIEW,
            TaskStatus.GENERATING,
            TaskStatus.DELETING,
        },
        TaskStatus.DELETING: set(),
    }

    def transition(
        self,
        current: TaskStatus,
        target: TaskStatus,
        *,
        current_version: int,
        expected_version: int,
    ) -> TaskStatus:
        if current_version != expected_version:
            raise VersionConflict(
                f"expected task version {expected_version}, current version is {current_version}"
            )
        if target not in self._allowed[current]:
            raise InvalidTransition(f"task cannot transition from {current} to {target}")
        return target
