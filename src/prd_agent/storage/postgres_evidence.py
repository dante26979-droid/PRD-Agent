"""PostgreSQL persistence for Tool Call, Evidence and deterministic Facts."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from prd_agent.hashing import sha256_json

from prd_agent.application.repository_evidence_service import (
    RepositoryToolExecution,
    ToolCallRecord,
)
from prd_agent.evidence.models import RepositoryEvidenceBundle
from prd_agent.tools.models import ToolResult


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _decoded(value):
    return json.loads(value) if isinstance(value, str) else value


class PostgresEvidenceStore:
    def __init__(self, connection) -> None:
        self.connection = connection

    @classmethod
    def from_dsn(cls, dsn: str) -> "PostgresEvidenceStore":
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError(
                "PostgreSQL evidence support requires: python -m pip install '.[postgres]'"
            ) from exc
        return cls(psycopg.connect(dsn))

    def get_replay(self, actor_id: str, idempotency_key: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT i.input_hash, c.call_json, c.tool_result_json, c.bundle_json
                  FROM tool_idempotency_records i
                  JOIN tool_calls c ON c.tool_call_id = i.tool_call_id
                 WHERE i.actor_id = %s AND i.idempotency_key = %s
                """,
                (actor_id, idempotency_key),
            )
            row = cursor.fetchone()
        if not row or row[2] is None or row[3] is None:
            return None
        execution = RepositoryToolExecution(
            tool_call=ToolCallRecord.model_validate(_decoded(row[1])),
            tool_result=ToolResult.model_validate(_decoded(row[2])),
            bundle=RepositoryEvidenceBundle.model_validate(_decoded(row[3])),
        )
        return row[0], execution

    def save_snapshot(self, snapshot) -> None:
        snapshot_id = "snapshot-" + sha256_json(
            {
                "repository_id": snapshot.repository_id,
                "resolved_commit_sha": snapshot.resolved_commit_sha,
                "allowed_prefix": snapshot.allowed_prefix,
            }
        ).split(":", 1)[1][:24]
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO repository_snapshots (
                    snapshot_id, repository_id, resolved_commit_sha,
                    allowed_prefix, resolver_version, resolved_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (repository_id, resolved_commit_sha, allowed_prefix)
                DO NOTHING
                """,
                (
                    snapshot_id,
                    snapshot.repository_id,
                    snapshot.resolved_commit_sha,
                    snapshot.allowed_prefix,
                    snapshot.resolver_version,
                    snapshot.resolved_at,
                ),
            )
        self.connection.commit()

    def save_running(self, call: ToolCallRecord, input_hash: str) -> None:
        payload = call.model_dump(mode="json")
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO tool_calls (
                    tool_call_id, actor_id, task_id, run_id, unit_id,
                    investigation_id, attempt, retry_of_tool_call_id,
                    repository_id, resolved_commit_sha, tool_id,
                    tool_schema_version, policy_version, purpose,
                    arguments_json, action_signature, idempotency_key,
                    status, public_summary, error_code, call_json,
                    started_at, ended_at, source_kind, source_id, source_version
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    call.tool_call_id,
                    call.actor_id,
                    call.task_id,
                    call.run_id,
                    call.unit_id,
                    call.investigation_id,
                    call.attempt,
                    call.retry_of_tool_call_id,
                    call.repository_id,
                    call.resolved_commit_sha,
                    call.tool_id,
                    call.tool_schema_version,
                    call.policy_version,
                    call.purpose,
                    _json(call.arguments),
                    call.action_signature,
                    call.idempotency_key,
                    call.status.value,
                    call.public_summary,
                    call.error_code,
                    _json(payload),
                    call.started_at,
                    call.ended_at,
                    "CODE_REPOSITORY",
                    call.repository_id,
                    call.resolved_commit_sha,
                ),
            )
            cursor.execute(
                """
                INSERT INTO tool_idempotency_records (
                    actor_id, idempotency_key, input_hash, tool_call_id
                ) VALUES (%s, %s, %s, %s)
                """,
                (call.actor_id, call.idempotency_key, input_hash, call.tool_call_id),
            )
        self.connection.commit()

    def recover_stale_running(self, *, stale_before) -> tuple[str, ...]:
        """Converge abandoned RUNNING calls without inventing tool results."""
        recovered: list[str] = []
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT tool_call_id, call_json
                  FROM tool_calls
                 WHERE status = 'RUNNING' AND started_at < %s
                 FOR UPDATE
                """,
                (stale_before,),
            )
            for tool_call_id, call_json in cursor.fetchall():
                call = ToolCallRecord.model_validate(_decoded(call_json)).model_copy(
                    update={
                        "status": "FAILED",
                        "public_summary": "工具调用执行进程失联",
                        "error_code": "WORKER_LOST",
                        "ended_at": datetime.now(timezone.utc),
                    }
                )
                cursor.execute(
                    """
                    UPDATE tool_calls
                       SET status = 'FAILED', public_summary = %s,
                           error_code = 'WORKER_LOST', call_json = %s,
                           ended_at = %s
                     WHERE tool_call_id = %s AND status = 'RUNNING'
                    """,
                    (
                        call.public_summary,
                        _json(call.model_dump(mode="json")),
                        call.ended_at,
                        tool_call_id,
                    ),
                )
                if cursor.rowcount:
                    recovered.append(tool_call_id)
        self.connection.commit()
        return tuple(recovered)

    def complete(
        self, actor_id: str, idempotency_key: str, input_hash: str, execution
    ) -> None:
        call = execution.tool_call
        result = execution.tool_result
        bundle = execution.bundle
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE tool_calls
                   SET status = %s, public_summary = %s, error_code = %s,
                       call_json = %s, tool_result_json = %s, bundle_json = %s,
                       ended_at = %s
                 WHERE tool_call_id = %s
                """,
                (
                    call.status.value,
                    call.public_summary,
                    call.error_code,
                    _json(call.model_dump(mode="json")),
                    _json(result.model_dump(mode="json")),
                    _json(bundle.model_dump(mode="json")),
                    call.ended_at,
                    call.tool_call_id,
                ),
            )
            for evidence in bundle.evidence:
                cursor.execute(
                    """
                    INSERT INTO source_evidence (
                        evidence_id, tool_call_id, source_type, repository_id,
                        resolved_commit_sha, path, line_start, line_end, symbol,
                        excerpt, content_hash, source_blob_id, extraction_method,
                        redaction_applied, retrieved_at, source_kind, source_id,
                        source_version, locator_json, access_scope_hash
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        evidence.evidence_id,
                        evidence.tool_call_id,
                        evidence.source_type,
                        evidence.repository_id,
                        evidence.resolved_commit_sha,
                        evidence.path,
                        evidence.line_start,
                        evidence.line_end,
                        evidence.symbol,
                        evidence.excerpt,
                        evidence.content_hash,
                        evidence.source_blob_id,
                        evidence.extraction_method.value,
                        evidence.redaction_applied,
                        evidence.retrieved_at,
                        evidence.source_kind.value,
                        evidence.source_id,
                        evidence.source_version,
                        _json(evidence.locator),
                        evidence.access_scope_hash,
                    ),
                )
            for fact in bundle.facts:
                cursor.execute(
                    """
                    INSERT INTO verified_facts (
                        fact_id, task_id, tool_call_id, investigation_id, subject, predicate,
                        value_json, fact_scope, fact_type, confidence,
                        verification_status, extractor_id, extractor_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        fact.fact_id,
                        fact.task_id,
                        fact.tool_call_id,
                        call.investigation_id,
                        fact.subject,
                        fact.predicate,
                        _json(fact.value_json),
                        fact.fact_scope.value,
                        fact.fact_type.value,
                        fact.confidence,
                        fact.verification_status.value,
                        fact.extractor_id,
                        fact.extractor_version,
                    ),
                )
                for evidence_id in fact.evidence_ids:
                    cursor.execute(
                        """
                        INSERT INTO fact_evidence_links (fact_id, evidence_id)
                        VALUES (%s, %s)
                        """,
                        (fact.fact_id, evidence_id),
                    )
            for unknown in bundle.unknowns:
                cursor.execute(
                    """
                    INSERT INTO unknown_items (
                        unknown_id, task_id, tool_call_id, investigation_id, statement, reason,
                        severity, resolution_type
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        unknown.unknown_id,
                        unknown.task_id,
                        unknown.tool_call_id,
                        call.investigation_id,
                        unknown.statement,
                        unknown.reason.value,
                        unknown.severity,
                        unknown.resolution_type,
                    ),
                )
            for conflict in bundle.conflicts:
                cursor.execute(
                    """
                    INSERT INTO source_conflicts (
                        conflict_id, investigation_id, subject, description, status
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        conflict.conflict_id,
                        call.investigation_id,
                        conflict.subject,
                        conflict.description,
                        conflict.status,
                    ),
                )
                for fact_id in conflict.fact_ids:
                    cursor.execute(
                        """
                        INSERT INTO source_conflict_facts (conflict_id, fact_id)
                        VALUES (%s, %s)
                        """,
                        (conflict.conflict_id, fact_id),
                    )
        self.connection.commit()

    def get_execution(self, tool_call_id: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT call_json, tool_result_json, bundle_json
                  FROM tool_calls WHERE tool_call_id = %s
                """,
                (tool_call_id,),
            )
            row = cursor.fetchone()
        if not row or row[1] is None or row[2] is None:
            raise KeyError(tool_call_id)
        return RepositoryToolExecution(
            tool_call=ToolCallRecord.model_validate(_decoded(row[0])),
            tool_result=ToolResult.model_validate(_decoded(row[1])),
            bundle=RepositoryEvidenceBundle.model_validate(_decoded(row[2])),
        )

    def all_calls(self):
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT call_json FROM tool_calls ORDER BY started_at, tool_call_id")
            return tuple(
                ToolCallRecord.model_validate(_decoded(row[0]))
                for row in cursor.fetchall()
            )

    def executions_for_investigation(self, investigation_id: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT call_json, tool_result_json, bundle_json
                  FROM tool_calls
                 WHERE investigation_id = %s
                   AND tool_result_json IS NOT NULL
                   AND bundle_json IS NOT NULL
                 ORDER BY started_at, tool_call_id
                """,
                (investigation_id,),
            )
            rows = cursor.fetchall()
        return tuple(
            RepositoryToolExecution(
                tool_call=ToolCallRecord.model_validate(_decoded(row[0])),
                tool_result=ToolResult.model_validate(_decoded(row[1])),
                bundle=RepositoryEvidenceBundle.model_validate(_decoded(row[2])),
            )
            for row in rows
        )
