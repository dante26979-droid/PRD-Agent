from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field

from prd_agent.evidence.conflict_detector import DeterministicConflictDetector
from prd_agent.evidence.deterministic_validator import EvidenceValidationError
from prd_agent.evidence.fact_builder import DeterministicFactBuilder
from prd_agent.evidence.models import RepositoryEvidenceBundle
from prd_agent.evidence.normalizer import EvidenceNormalizer
from prd_agent.hashing import sha256_json
from prd_agent.repository.content_policy import RepositoryContentPolicy
from prd_agent.tools.models import ToolAction, ToolResult, ToolStatus
from prd_agent.tools.registry import action_signature


class EvidenceServiceError(Exception):
    pass


class IdempotencyConflict(EvidenceServiceError):
    pass


class ToolCallStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    EMPTY = "EMPTY"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class ToolCallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: str
    actor_id: str
    task_id: str | None = None
    run_id: str | None = None
    unit_id: str | None = None
    investigation_id: str | None = None
    attempt: int = Field(default=1, ge=1)
    retry_of_tool_call_id: str | None = None
    repository_id: str
    resolved_commit_sha: str
    tool_id: str
    tool_schema_version: str
    policy_version: str = "repository-tools.v1"
    purpose: str
    arguments: dict
    action_signature: str
    idempotency_key: str
    status: ToolCallStatus
    public_summary: str = ""
    error_code: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None


class RepositoryToolExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call: ToolCallRecord
    tool_result: ToolResult
    bundle: RepositoryEvidenceBundle


class RepositoryEvidenceService:
    def __init__(self, reader, registry, store, validator) -> None:
        self.reader = reader
        self.registry = registry
        self.store = store
        self.validator = validator
        self.normalizer = EvidenceNormalizer()
        self.fact_builder = DeterministicFactBuilder()
        self.conflict_detector = DeterministicConflictDetector()
        self.content_policy = RepositoryContentPolicy()

    def resolve_snapshot(self, repository_id: str, revision: str | None = None):
        snapshot = self.reader.resolve_snapshot(repository_id, revision)
        save_snapshot = getattr(self.store, "save_snapshot", None)
        if save_snapshot:
            save_snapshot(snapshot)
        return snapshot

    def execute(
        self,
        action: ToolAction,
        *,
        actor_id: str,
        idempotency_key: str,
        task_id: str | None = None,
        run_id: str | None = None,
        unit_id: str | None = None,
        investigation_id: str | None = None,
        attempt: int = 1,
        retry_of_tool_call_id: str | None = None,
    ) -> RepositoryToolExecution:
        input_hash = sha256_json(
            {
                "action": action.model_dump(mode="json"),
                "task_id": task_id,
                "run_id": run_id,
                "unit_id": unit_id,
                "investigation_id": investigation_id,
                "attempt": attempt,
                "retry_of_tool_call_id": retry_of_tool_call_id,
            }
        )
        replay = self.store.get_replay(actor_id, idempotency_key)
        if replay:
            replay_hash, execution = replay
            if replay_hash != input_hash:
                raise IdempotencyConflict(
                    "idempotency key was already used with different input"
                )
            return execution

        validated = self.registry.validate(action)
        snapshot = self.reader.resolve_snapshot(
            action.repository_id, action.resolved_commit_sha
        )
        save_snapshot = getattr(self.store, "save_snapshot", None)
        if save_snapshot:
            save_snapshot(snapshot)
        call = ToolCallRecord(
            tool_call_id=f"tool-call-{uuid.uuid4().hex}",
            actor_id=actor_id,
            task_id=task_id,
            run_id=run_id,
            unit_id=unit_id,
            investigation_id=investigation_id,
            attempt=attempt,
            retry_of_tool_call_id=retry_of_tool_call_id,
            repository_id=action.repository_id,
            resolved_commit_sha=snapshot.resolved_commit_sha,
            tool_id=action.tool_id,
            tool_schema_version=action.tool_schema_version,
            purpose=action.purpose,
            arguments=action.arguments,
            action_signature=action_signature(action),
            idempotency_key=idempotency_key,
            status=ToolCallStatus.RUNNING,
        )
        self.store.save_running(call, input_hash)
        tool_started = time.perf_counter()
        try:
            result = validated.handler.execute(snapshot, validated.arguments)
        except Exception:  # noqa: BLE001 - provider details must not cross the boundary
            result = ToolResult(
                status=ToolStatus.FAILED,
                public_summary="仓库工具执行失败",
                error_code="TOOL_EXECUTION_FAILED",
            )
        result = self._sanitize_result(result)
        result = result.model_copy(
            update={"duration_ms": int((time.perf_counter() - tool_started) * 1000)}
        )
        try:
            bundle = self.normalizer.normalize(
                call.tool_call_id, action, result, task_id=task_id
            )
            for evidence in bundle.evidence:
                self.validator.validate(snapshot, evidence)
            facts = self.fact_builder.build(
                action, result, bundle.evidence, task_id=task_id
            )
            facts, conflicts = self.conflict_detector.detect(facts)
            bundle = bundle.model_copy(
                update={"facts": facts, "conflicts": conflicts}
            )
        except EvidenceValidationError:
            result = ToolResult(
                status=ToolStatus.FAILED,
                public_summary="Evidence 定位或内容校验失败",
                error_code="EVIDENCE_VALIDATION_FAILED",
            )
            bundle = self.normalizer.normalize(
                call.tool_call_id, action, result, task_id=task_id
            )
        except Exception:  # noqa: BLE001 - processing details must not cross the boundary
            result = ToolResult(
                status=ToolStatus.FAILED,
                public_summary="仓库证据处理失败",
                error_code="TOOL_PROCESSING_FAILED",
            )
            bundle = RepositoryEvidenceBundle(
                tool_call_id=call.tool_call_id,
            )
        call = call.model_copy(
            update={
                "status": ToolCallStatus(result.status.value),
                "public_summary": result.public_summary,
                "error_code": result.error_code,
                "ended_at": datetime.now(timezone.utc),
            }
        )
        execution = RepositoryToolExecution(
            tool_call=call,
            tool_result=result,
            bundle=bundle,
        )
        self.store.complete(actor_id, idempotency_key, input_hash, execution)
        return execution

    def get_bundle(self, tool_call_id: str) -> RepositoryToolExecution:
        return self.store.get_execution(tool_call_id)

    def _sanitize_result(self, result: ToolResult) -> ToolResult:
        """Apply the public content policy after every tool, before persistence."""
        sanitized_items = []
        changed = False
        for item in result.items:
            excerpt = item.excerpt
            excerpt_changed = False
            if excerpt is not None:
                excerpt, excerpt_changed = self.content_policy.redact(excerpt)
            metadata, metadata_changed = self.content_policy.redact_structure(
                item.metadata
            )
            item_changed = excerpt_changed or metadata_changed
            if item_changed:
                metadata = dict(metadata)
                metadata["redaction_applied"] = True
                sanitized_items.append(
                    item.model_copy(
                        update={"excerpt": excerpt, "metadata": metadata}
                    )
                )
            else:
                sanitized_items.append(item)
            changed = changed or item_changed
        if not changed:
            return result
        return result.model_copy(update={"items": tuple(sanitized_items)})
