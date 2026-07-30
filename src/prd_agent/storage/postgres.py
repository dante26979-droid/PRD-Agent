"""PostgreSQL business-state repository for the M0 Step 2 workflow.

The optional psycopg dependency is imported only by ``from_dsn`` so the offline
unit-test and evaluation paths remain dependency-free.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from typing import Any
import uuid

from prd_agent.domain.entities import (
    AgentRun,
    ConfirmationUnit,
    DomainEvent,
    IdempotencyRecord,
    OutlineNode,
    OutlineVersion,
    PrdDocumentVersion,
    PrdSectionVersion,
    RequirementBrief,
    RequirementBriefVersion,
    Task,
    TaskMessage,
    WorkflowSnapshot,
)
from prd_agent.domain.enums import (
    Complexity,
    DocumentStatus,
    OutlineStatus,
    RunStatus,
    SectionStatus,
    TaskStatus,
    UnitStatus,
)
from prd_agent.domain.errors import NotFound, VersionConflict
from prd_agent.grounding.models import GroundingResult
from prd_agent.quality.models import QualityResult


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _decoded(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class PostgresWorkflowRepository:
    def __init__(self, connection) -> None:
        self.connection = connection

    @classmethod
    def from_dsn(cls, dsn: str) -> "PostgresWorkflowRepository":
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError(
                "PostgreSQL workflow support requires: python -m pip install '.[postgres]'"
            ) from exc
        return cls(psycopg.connect(dsn))

    def commit(self) -> None:
        self.connection.commit()

    @contextmanager
    def idempotency_lock(self, actor_id: str, key: str):
        digest = hashlib.sha256(
            f"{actor_id}\0{key}".encode("utf-8")
        ).digest()
        advisory_key = int.from_bytes(digest[:8], "big", signed=True)
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (advisory_key,))
        try:
            yield
        finally:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (advisory_key,),
                )

    def get_idempotency(self, actor_id: str, key: str) -> IdempotencyRecord | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT actor_id, idempotency_key, input_hash, task_id, run_id, created_at
                  FROM idempotency_records
                 WHERE actor_id = %s AND idempotency_key = %s
                """,
                (actor_id, key),
            )
            row = cursor.fetchone()
        return IdempotencyRecord(*row) if row else None

    def save_idempotency(self, value: IdempotencyRecord) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO idempotency_records (
                    actor_id, idempotency_key, input_hash, task_id, run_id, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (actor_id, idempotency_key) DO NOTHING
                """,
                (
                    value.actor_id,
                    value.idempotency_key,
                    value.input_hash,
                    value.task_id,
                    value.run_id,
                    value.created_at,
                ),
            )

    def create_task_bundle(
        self,
        task: Task,
        message: TaskMessage,
        brief: RequirementBriefVersion,
        run: AgentRun,
        record: IdempotencyRecord,
    ) -> None:
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO prd_tasks (
                        task_id, title, status, version, current_outline_version,
                        current_unit_sequence, created_at, updated_at, owner_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    self._task_values(task),
                )
                cursor.execute(
                    """
                    INSERT INTO task_messages (
                        message_id, task_id, actor_id, content, created_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        message.message_id,
                        message.task_id,
                        message.actor_id,
                        message.content,
                        message.created_at,
                    ),
                )
                self._insert_brief(cursor, brief)
                self._upsert_run(cursor, run)
                cursor.execute(
                    """
                    INSERT INTO idempotency_records (
                        actor_id, idempotency_key, input_hash, task_id, run_id, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record.actor_id,
                        record.idempotency_key,
                        record.input_hash,
                        record.task_id,
                        record.run_id,
                        record.created_at,
                    ),
                )

    def get_task(self, task_id: str) -> Task:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT task_id, title, status, version, current_outline_version,
                       current_unit_sequence, created_at, updated_at, owner_id
                  FROM prd_tasks WHERE task_id = %s
                """,
                (task_id,),
            )
            row = cursor.fetchone()
        if not row:
            raise NotFound(f"task not found: {task_id}")
        return Task(
            task_id=row[0],
            title=row[1],
            status=TaskStatus(row[2]),
            version=row[3],
            current_outline_version=row[4],
            current_unit_sequence=row[5],
            created_at=row[6],
            updated_at=row[7],
            owner_id=row[8],
        )

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
        parameters: list[Any] = [owner_id]
        cursor_clause = ""
        if before is not None:
            cursor_clause = "AND (updated_at, task_id) < (%s, %s)"
            parameters.extend(before)
        parameters.append(limit)
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT task_id, title, status, version, current_outline_version,
                       current_unit_sequence, created_at, updated_at, owner_id
                  FROM prd_tasks
                 WHERE owner_id = %s
                   {cursor_clause}
                 ORDER BY updated_at DESC, task_id DESC
                 LIMIT %s
                """,
                tuple(parameters),
            )
            rows = cursor.fetchall()
        return tuple(
            Task(
                task_id=row[0],
                title=row[1],
                status=TaskStatus(row[2]),
                version=row[3],
                current_outline_version=row[4],
                current_unit_sequence=row[5],
                created_at=row[6],
                updated_at=row[7],
                owner_id=row[8],
            )
            for row in rows
        )

    def save_task(self, task: Task) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE prd_tasks
                   SET title = %s, status = %s, version = %s,
                       current_outline_version = %s, current_unit_sequence = %s,
                       updated_at = %s
                 WHERE task_id = %s AND version = %s
                """,
                (
                    task.title,
                    task.status.value,
                    task.version,
                    task.current_outline_version,
                    task.current_unit_sequence,
                    task.updated_at,
                    task.task_id,
                    task.version - 1,
                ),
            )
            if cursor.rowcount != 1:
                raise VersionConflict(f"stale write for task: {task.task_id}")

    def save_run(self, run: AgentRun) -> None:
        with self.connection.cursor() as cursor:
            self._upsert_run(cursor, run)

    def latest_run(self, task_id: str) -> AgentRun:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run_id, task_id, thread_id, graph_name, graph_version,
                       status, input_hash, idempotency_key, started_at, ended_at, error
                  FROM agent_runs
                 WHERE task_id = %s
                 ORDER BY COALESCE(started_at, ended_at) DESC, run_id DESC
                 LIMIT 1
                """,
                (task_id,),
            )
            row = cursor.fetchone()
        if not row:
            raise NotFound(f"run not found for task: {task_id}")
        return self._run_from_row(row)

    def save_brief(self, value: RequirementBriefVersion) -> None:
        with self.connection.cursor() as cursor:
            self._insert_brief(cursor, value)

    def save_outline(self, value: OutlineVersion) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO outline_versions (
                    outline_id, task_id, version, title, status, created_at, confirmed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (outline_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    status = EXCLUDED.status,
                    confirmed_at = EXCLUDED.confirmed_at
                """,
                (
                    value.outline_id,
                    value.task_id,
                    value.version,
                    value.title,
                    value.status.value,
                    value.created_at,
                    value.confirmed_at,
                ),
            )
            for node in value.nodes:
                cursor.execute(
                    """
                    INSERT INTO outline_nodes (
                        node_id, outline_id, sequence, title, purpose,
                        complexity, required_information, parent_id, level, stable_key
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (node_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        purpose = EXCLUDED.purpose,
                        complexity = EXCLUDED.complexity,
                        required_information = EXCLUDED.required_information,
                        parent_id = EXCLUDED.parent_id,
                        level = EXCLUDED.level,
                        stable_key = EXCLUDED.stable_key
                    """,
                    (
                        node.node_id,
                        value.outline_id,
                        node.sequence,
                        node.title,
                        node.purpose,
                        node.complexity.value,
                        _json(list(node.required_information)),
                        node.parent_id,
                        node.level,
                        node.stable_key,
                    ),
                )
            for unit in value.confirmation_units:
                cursor.execute(
                    """
                    INSERT INTO confirmation_units (
                        unit_id, outline_id, sequence, title, status, content, version,
                        node_ids_json, depends_on_unit_ids_json, content_hash,
                        grounding_run_id, quality_run_id, section_drafts_json
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (unit_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        content = EXCLUDED.content,
                        version = EXCLUDED.version,
                        node_ids_json = EXCLUDED.node_ids_json,
                        depends_on_unit_ids_json = EXCLUDED.depends_on_unit_ids_json,
                        content_hash = EXCLUDED.content_hash,
                        grounding_run_id = EXCLUDED.grounding_run_id,
                        quality_run_id = EXCLUDED.quality_run_id,
                        section_drafts_json = EXCLUDED.section_drafts_json
                    """,
                    (
                        unit.unit_id,
                        unit.outline_id,
                        unit.sequence,
                        unit.title,
                        unit.status.value,
                        unit.content,
                        unit.version,
                        _json(list(unit.node_ids)),
                        _json(list(unit.depends_on_unit_ids)),
                        unit.content_hash,
                        unit.grounding_run_id,
                        unit.quality_run_id,
                        _json(list(unit.section_drafts)),
                    ),
                )

    def save_section(self, task_id: str, value: PrdSectionVersion) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO prd_section_versions (
                    section_id, task_id, unit_id, version, title, content, created_at,
                    node_id, status, content_hash, source_run_id, grounding_run_id,
                    quality_run_id, supersedes_section_id, confirmed_by, confirmed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    value.section_id,
                    task_id,
                    value.unit_id,
                    value.version,
                    value.title,
                    value.content,
                    value.created_at,
                    value.node_id,
                    value.status.value,
                    value.content_hash,
                    value.source_run_id,
                    value.grounding_run_id,
                    value.quality_run_id,
                    value.supersedes_section_id,
                    value.confirmed_by,
                    value.confirmed_at,
                ),
            )

    def save_document(self, value: PrdDocumentVersion) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO prd_document_versions (
                    document_id, task_id, version, markdown, created_at,
                    content_hash, status, section_ids_json, confirmed_by, confirmed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (document_id) DO UPDATE SET
                    content_hash = EXCLUDED.content_hash,
                    status = EXCLUDED.status,
                    section_ids_json = EXCLUDED.section_ids_json,
                    confirmed_by = EXCLUDED.confirmed_by,
                    confirmed_at = EXCLUDED.confirmed_at
                """,
                (
                    value.document_id,
                    value.task_id,
                    value.version,
                    value.markdown,
                    value.created_at,
                    value.content_hash,
                    value.status.value,
                    _json(list(value.section_ids)),
                    value.confirmed_by,
                    value.confirmed_at,
                ),
            )

    def save_grounding_result(self, task_id: str, value: GroundingResult) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO grounding_results (
                    grounding_run_id, task_id, unit_id, payload
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (grounding_run_id) DO UPDATE SET
                    payload = EXCLUDED.payload
                """,
                (
                    value.grounding_run_id,
                    task_id,
                    value.unit_id,
                    _json(value.model_dump(mode="json")),
                ),
            )

    def save_quality_result(self, task_id: str, value: QualityResult) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO quality_results (
                    quality_run_id, task_id, scope_id, payload
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (quality_run_id) DO UPDATE SET
                    payload = EXCLUDED.payload
                """,
                (
                    value.quality_run_id,
                    task_id,
                    value.scope_id,
                    _json(value.model_dump(mode="json")),
                ),
            )

    def save_message(self, value: TaskMessage) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO task_messages (
                    message_id, task_id, actor_id, content, created_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    value.message_id,
                    value.task_id,
                    value.actor_id,
                    value.content,
                    value.created_at,
                ),
            )

    def save_event(self, value: DomainEvent) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO domain_events (
                    event_id, task_id, sequence, event_type, payload, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    value.event_id,
                    value.task_id,
                    value.sequence,
                    value.event_type,
                    _json(dict(value.payload)),
                    value.created_at,
                ),
            )

    def append_event(self, task_id: str, event_type: str, payload: dict) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT task_id FROM prd_tasks WHERE task_id = %s FOR UPDATE",
                (task_id,),
            )
            cursor.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM domain_events WHERE task_id = %s",
                (task_id,),
            )
            sequence = cursor.fetchone()[0]
        self.save_event(
            DomainEvent(
                event_id=f"event-{uuid.uuid4().hex}",
                task_id=task_id,
                sequence=sequence,
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
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_id, task_id, sequence, event_type, payload, created_at
                  FROM domain_events
                 WHERE task_id = %s AND sequence > %s
                 ORDER BY sequence
                 LIMIT %s
                """,
                (task_id, after_sequence, limit),
            )
            rows = cursor.fetchall()
        return tuple(
            DomainEvent(
                event_id=row[0],
                task_id=row[1],
                sequence=row[2],
                event_type=row[3],
                payload=_decoded(row[4]),
                created_at=row[5],
            )
            for row in rows
        )

    def latest_event_sequence(self, task_id: str, owner_id: str) -> int:
        self.snapshot_for_owner(task_id, owner_id)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(MAX(sequence), 0)
                  FROM domain_events
                 WHERE task_id = %s
                """,
                (task_id,),
            )
            return int(cursor.fetchone()[0])

    def save_checkpoint(self, thread_id: str, value: dict) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO workflow_checkpoints (
                    thread_id, task_id, graph_version, task_version, state
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (thread_id) DO UPDATE SET
                    task_id = EXCLUDED.task_id,
                    graph_version = EXCLUDED.graph_version,
                    task_version = EXCLUDED.task_version,
                    state = EXCLUDED.state,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    thread_id,
                    value["task_id"],
                    value["graph_version"],
                    value["task_version"],
                    _json(value),
                ),
            )
        self.connection.commit()

    def get_checkpoint(self, thread_id: str) -> dict | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM workflow_checkpoints WHERE thread_id = %s",
                (thread_id,),
            )
            row = cursor.fetchone()
        return _decoded(row[0]) if row else None

    def snapshot(self, task_id: str) -> WorkflowSnapshot:
        task = self.get_task(task_id)
        run = self.latest_run(task_id)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT version, payload, confirmed, created_at
                  FROM requirement_brief_versions
                 WHERE task_id = %s ORDER BY version
                """,
                (task_id,),
            )
            briefs = tuple(
                RequirementBriefVersion(
                    task_id,
                    row[0],
                    RequirementBrief.from_mapping(_decoded(row[1])),
                    row[2],
                    row[3],
                )
                for row in cursor.fetchall()
            )
            cursor.execute(
                """
                SELECT outline_id, version, title, status, created_at, confirmed_at
                  FROM outline_versions
                 WHERE task_id = %s ORDER BY version
                """,
                (task_id,),
            )
            outlines = tuple(self._load_outline(cursor, task_id, row) for row in cursor.fetchall())
            cursor.execute(
                """
                SELECT section_id, unit_id, version, title, content, created_at,
                       node_id, status, content_hash, source_run_id, grounding_run_id,
                       quality_run_id, supersedes_section_id, confirmed_by, confirmed_at
                  FROM prd_section_versions
                 WHERE task_id = %s ORDER BY created_at, version
                """,
                (task_id,),
            )
            sections = tuple(
                PrdSectionVersion(
                    section_id=row[0],
                    unit_id=row[1],
                    version=row[2],
                    title=row[3],
                    content=row[4],
                    created_at=row[5],
                    node_id=row[6],
                    status=SectionStatus(row[7]),
                    content_hash=row[8],
                    source_run_id=row[9],
                    grounding_run_id=row[10],
                    quality_run_id=row[11],
                    supersedes_section_id=row[12],
                    confirmed_by=row[13],
                    confirmed_at=row[14],
                )
                for row in cursor.fetchall()
            )
            cursor.execute(
                """
                SELECT document_id, version, markdown, created_at, content_hash,
                       status, section_ids_json, confirmed_by, confirmed_at
                  FROM prd_document_versions
                 WHERE task_id = %s ORDER BY version
                """,
                (task_id,),
            )
            documents = tuple(
                PrdDocumentVersion(
                    document_id=row[0],
                    task_id=task_id,
                    version=row[1],
                    markdown=row[2],
                    created_at=row[3],
                    content_hash=row[4],
                    status=DocumentStatus(row[5]),
                    section_ids=tuple(_decoded(row[6]) or []),
                    confirmed_by=row[7],
                    confirmed_at=row[8],
                )
                for row in cursor.fetchall()
            )
            cursor.execute(
                """
                SELECT message_id, actor_id, content, created_at
                  FROM task_messages
                 WHERE task_id = %s ORDER BY created_at, message_id
                """,
                (task_id,),
            )
            messages = tuple(
                TaskMessage(row[0], task_id, row[1], row[2], row[3])
                for row in cursor.fetchall()
            )
            cursor.execute(
                """
                SELECT payload FROM quality_results
                 WHERE task_id = %s ORDER BY created_at, quality_run_id
                """,
                (task_id,),
            )
            quality_results = tuple(
                QualityResult.model_validate(_decoded(row[0]))
                for row in cursor.fetchall()
            )
            cursor.execute(
                """
                SELECT payload FROM grounding_results
                 WHERE task_id = %s ORDER BY created_at, grounding_run_id
                """,
                (task_id,),
            )
            grounding_results = tuple(
                GroundingResult.model_validate(_decoded(row[0]))
                for row in cursor.fetchall()
            )
        return WorkflowSnapshot(
            task,
            run,
            briefs,
            outlines,
            sections,
            documents,
            messages,
            quality_results,
            grounding_results,
        )

    @staticmethod
    def _task_values(task: Task) -> tuple[Any, ...]:
        return (
            task.task_id,
            task.title,
            task.status.value,
            task.version,
            task.current_outline_version,
            task.current_unit_sequence,
            task.created_at,
            task.updated_at,
            task.owner_id,
        )

    @staticmethod
    def _insert_brief(cursor, value: RequirementBriefVersion) -> None:
        cursor.execute(
            """
            INSERT INTO requirement_brief_versions (
                task_id, version, payload, confirmed, created_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                value.task_id,
                value.version,
                _json(value.brief.as_dict()),
                value.confirmed,
                value.created_at,
            ),
        )

    @staticmethod
    def _upsert_run(cursor, run: AgentRun) -> None:
        cursor.execute(
            """
            INSERT INTO agent_runs (
                run_id, task_id, thread_id, graph_name, graph_version, status,
                input_hash, idempotency_key, started_at, ended_at, error
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = EXCLUDED.status,
                started_at = EXCLUDED.started_at,
                ended_at = EXCLUDED.ended_at,
                error = EXCLUDED.error
            """,
            (
                run.run_id,
                run.task_id,
                run.thread_id,
                run.graph_name,
                run.graph_version,
                run.status.value,
                run.input_hash,
                run.idempotency_key,
                run.started_at,
                run.ended_at,
                run.error,
            ),
        )

    @staticmethod
    def _run_from_row(row: Any) -> AgentRun:
        return AgentRun(
            run_id=row[0],
            task_id=row[1],
            thread_id=row[2],
            graph_name=row[3],
            graph_version=row[4],
            status=RunStatus(row[5]),
            input_hash=row[6],
            idempotency_key=row[7],
            started_at=row[8],
            ended_at=row[9],
            error=row[10],
        )

    @staticmethod
    def _load_outline(cursor, task_id: str, row: Any) -> OutlineVersion:
        outline_id = row[0]
        cursor.execute(
            """
            SELECT node_id, sequence, title, purpose, complexity, required_information,
                   parent_id, level, stable_key
              FROM outline_nodes WHERE outline_id = %s ORDER BY sequence
            """,
            (outline_id,),
        )
        nodes = tuple(
            OutlineNode(
                item[0],
                item[1],
                item[2],
                item[3],
                Complexity(item[4]),
                tuple(_decoded(item[5]) or []),
                item[6],
                item[7],
                item[8],
            )
            for item in cursor.fetchall()
        )
        cursor.execute(
            """
            SELECT unit_id, sequence, title, status, content, version,
                   node_ids_json, depends_on_unit_ids_json, content_hash,
                   grounding_run_id, quality_run_id, section_drafts_json
              FROM confirmation_units WHERE outline_id = %s ORDER BY sequence
            """,
            (outline_id,),
        )
        units = tuple(
            ConfirmationUnit(
                item[0],
                outline_id,
                item[1],
                item[2],
                UnitStatus(item[3]),
                item[4],
                item[5],
                tuple(_decoded(item[6]) or []),
                tuple(_decoded(item[7]) or []),
                item[8],
                item[9],
                item[10],
                tuple(_decoded(item[11]) or []),
            )
            for item in cursor.fetchall()
        )
        return OutlineVersion(
            outline_id=outline_id,
            task_id=task_id,
            version=row[1],
            title=row[2],
            status=OutlineStatus(row[3]),
            nodes=nodes,
            confirmation_units=units,
            created_at=row[4],
            confirmed_at=row[5],
        )
