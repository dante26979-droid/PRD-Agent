from __future__ import annotations

from copy import deepcopy
from threading import RLock
import uuid

from prd_agent.domain.entities import (
    AgentRun,
    DomainEvent,
    IdempotencyRecord,
    OutlineVersion,
    PrdDocumentVersion,
    PrdSectionVersion,
    RequirementBriefVersion,
    Task,
    TaskMessage,
    WorkflowSnapshot,
)
from prd_agent.domain.enums import ACTIVE_RUN_STATUSES
from prd_agent.domain.errors import ActiveRunConflict, NotFound


class InMemoryWorkflowRepository:
    """Test adapter with the same observable contract as PostgreSQL storage."""

    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.runs: dict[str, AgentRun] = {}
        self.briefs: dict[str, list[RequirementBriefVersion]] = {}
        self.outlines: dict[str, list[OutlineVersion]] = {}
        self.sections: dict[str, list[PrdSectionVersion]] = {}
        self.documents: dict[str, list[PrdDocumentVersion]] = {}
        self.messages: dict[str, list[TaskMessage]] = {}
        self.quality_results: dict[str, list] = {}
        self.grounding_results: dict[str, list] = {}
        self.idempotency: dict[tuple[str, str], IdempotencyRecord] = {}
        self.events: dict[str, list[DomainEvent]] = {}
        self.checkpoints: dict[str, dict] = {}
        self._lock = RLock()

    def commit(self) -> None:
        """Match the PostgreSQL repository's unit-of-work boundary."""

    def get_idempotency(self, actor_id: str, key: str) -> IdempotencyRecord | None:
        return deepcopy(self.idempotency.get((actor_id, key)))

    def save_idempotency(self, record: IdempotencyRecord) -> None:
        self.idempotency[(record.actor_id, record.idempotency_key)] = deepcopy(record)

    def create_task_bundle(
        self,
        task: Task,
        message: TaskMessage,
        brief: RequirementBriefVersion,
        run: AgentRun,
        record: IdempotencyRecord,
    ) -> None:
        with self._lock:
            self.tasks[task.task_id] = deepcopy(task)
            self.messages[task.task_id] = [deepcopy(message)]
            self.briefs[task.task_id] = [deepcopy(brief)]
            self.outlines[task.task_id] = []
            self.sections[task.task_id] = []
            self.documents[task.task_id] = []
            self.events[task.task_id] = []
            self.quality_results[task.task_id] = []
            self.grounding_results[task.task_id] = []
            self.runs[run.run_id] = deepcopy(run)
            self.save_idempotency(record)

    def get_task(self, task_id: str) -> Task:
        if task_id not in self.tasks:
            raise NotFound(f"task not found: {task_id}")
        return deepcopy(self.tasks[task_id])

    def snapshot_for_owner(self, task_id: str, owner_id: str) -> WorkflowSnapshot:
        task = self.get_task(task_id)
        if task.owner_id != owner_id:
            raise NotFound(f"task not found: {task_id}")
        return self.snapshot(task_id)

    def list_tasks(
        self,
        owner_id: str,
        *,
        before: tuple | None = None,
        limit: int = 20,
    ) -> tuple[Task, ...]:
        values = [item for item in self.tasks.values() if item.owner_id == owner_id]
        values.sort(key=lambda item: (item.updated_at, item.task_id), reverse=True)
        if before is not None:
            values = [
                item
                for item in values
                if (item.updated_at, item.task_id) < before
            ]
        return tuple(deepcopy(values[:limit]))

    def save_task(self, task: Task) -> None:
        self.tasks[task.task_id] = deepcopy(task)

    def save_run(self, run: AgentRun) -> None:
        if run.status in ACTIVE_RUN_STATUSES:
            conflict = next(
                (
                    item
                    for item in self.runs.values()
                    if item.task_id == run.task_id
                    and item.run_id != run.run_id
                    and item.status in ACTIVE_RUN_STATUSES
                ),
                None,
            )
            if conflict:
                raise ActiveRunConflict(f"task already has active run: {conflict.run_id}")
        self.runs[run.run_id] = deepcopy(run)

    def latest_run(self, task_id: str) -> AgentRun:
        runs = [item for item in self.runs.values() if item.task_id == task_id]
        if not runs:
            raise NotFound(f"run not found for task: {task_id}")
        return deepcopy(sorted(runs, key=lambda item: item.started_at or item.ended_at)[-1])

    def save_brief(self, value: RequirementBriefVersion) -> None:
        self.briefs.setdefault(value.task_id, []).append(deepcopy(value))

    def save_outline(self, value: OutlineVersion) -> None:
        values = self.outlines.setdefault(value.task_id, [])
        for index, item in enumerate(values):
            if item.outline_id == value.outline_id:
                values[index] = deepcopy(value)
                return
        values.append(deepcopy(value))

    def save_section(self, task_id: str, value: PrdSectionVersion) -> None:
        self.sections.setdefault(task_id, []).append(deepcopy(value))

    def save_document(self, value: PrdDocumentVersion) -> None:
        values = self.documents.setdefault(value.task_id, [])
        for index, item in enumerate(values):
            if item.document_id == value.document_id:
                values[index] = deepcopy(value)
                return
        values.append(deepcopy(value))

    def save_message(self, value: TaskMessage) -> None:
        self.messages.setdefault(value.task_id, []).append(deepcopy(value))

    def save_quality_result(self, task_id: str, value) -> None:
        values = self.quality_results.setdefault(task_id, [])
        for index, item in enumerate(values):
            if item.quality_run_id == value.quality_run_id:
                values[index] = deepcopy(value)
                return
        values.append(deepcopy(value))

    def save_grounding_result(self, task_id: str, value) -> None:
        values = self.grounding_results.setdefault(task_id, [])
        for index, item in enumerate(values):
            if item.grounding_run_id == value.grounding_run_id:
                values[index] = deepcopy(value)
                return
        values.append(deepcopy(value))

    def save_event(self, value: DomainEvent) -> None:
        self.events.setdefault(value.task_id, []).append(deepcopy(value))

    def append_event(self, task_id: str, event_type: str, payload: dict) -> None:
        values = self.events.setdefault(task_id, [])
        self.save_event(
            DomainEvent(
                event_id=f"event-{uuid.uuid4().hex}",
                task_id=task_id,
                sequence=len(values) + 1,
                event_type=event_type,
                payload=payload,
            )
        )

    def list_events(
        self,
        task_id: str,
        owner_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[DomainEvent, ...]:
        self.snapshot_for_owner(task_id, owner_id)
        return tuple(
            deepcopy(
                [
                    item
                    for item in self.events.get(task_id, [])
                    if item.sequence > after_sequence
                ][:limit]
            )
        )

    def latest_event_sequence(self, task_id: str, owner_id: str) -> int:
        values = self.list_events(
            task_id,
            owner_id,
            after_sequence=0,
            limit=max(len(self.events.get(task_id, [])), 1),
        )
        return values[-1].sequence if values else 0

    def save_checkpoint(self, thread_id: str, value: dict) -> None:
        self.checkpoints[thread_id] = deepcopy(value)

    def get_checkpoint(self, thread_id: str) -> dict | None:
        return deepcopy(self.checkpoints.get(thread_id))

    def snapshot(self, task_id: str) -> WorkflowSnapshot:
        return WorkflowSnapshot(
            task=self.get_task(task_id),
            run=self.latest_run(task_id),
            briefs=tuple(deepcopy(self.briefs.get(task_id, []))),
            outlines=tuple(deepcopy(self.outlines.get(task_id, []))),
            sections=tuple(deepcopy(self.sections.get(task_id, []))),
            documents=tuple(deepcopy(self.documents.get(task_id, []))),
            messages=tuple(deepcopy(self.messages.get(task_id, []))),
            quality_results=tuple(deepcopy(self.quality_results.get(task_id, []))),
            grounding_results=tuple(
                deepcopy(self.grounding_results.get(task_id, []))
            ),
        )
