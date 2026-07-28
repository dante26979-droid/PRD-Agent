from __future__ import annotations

import base64
from dataclasses import asdict
from datetime import datetime
from enum import Enum
import json
from typing import Any

from prd_agent.domain.enums import RunStatus, TaskStatus, UnitStatus
from prd_agent.domain.errors import InvalidCommand, NotFound
from prd_agent.repository.content_policy import RepositoryContentPolicy

from .models import (
    AvailableActionView,
    DocumentView,
    ConflictView,
    CoverageItemView,
    EvidenceView,
    FactView,
    GroundingView,
    InvestigationStepView,
    InvestigationView,
    MessageView,
    OutlineNodeView,
    OutlineView,
    QualityIssueView,
    QualityView,
    RunView,
    TaskDetailResponse,
    TaskListResponse,
    TaskSummaryView,
    ToolCallView,
    UnknownView,
    UnitView,
)


PUBLIC_EVENT_NAMES = {
    "TaskStarted": "task.started",
    "ClarificationRequested": "task.input_required",
    "OutlineGenerated": "outline.ready_for_confirmation",
    "OutlineConfirmed": "outline.confirmed",
    "UnitGenerated": "unit.ready_for_confirmation",
    "UnitGroundingBlocked": "grounding.blocked",
    "UnitConfirmed": "unit.confirmed",
    "DocumentQualityChecked": "quality.checked",
    "RevisionPlanApproved": "revision.approved",
    "PrdRendered": "document.updated",
    "PrdFinalized": "document.finalized",
    "PrdReopened": "document.reopened",
    "RunFailed": "run.failed",
    "ExportPreviewed": "export.previewed",
    "ExportStarted": "export.started",
    "ExportSucceeded": "export.succeeded",
    "ExportFailed": "export.failed",
}

PUBLIC_EVENT_PAYLOAD_FIELDS = {
    "TaskStarted": frozenset({"run_id"}),
    "ClarificationRequested": frozenset({"question_count"}),
    "OutlineGenerated": frozenset({"outline_version"}),
    "OutlineConfirmed": frozenset({"outline_version"}),
    "UnitGenerated": frozenset({"unit_id"}),
    "UnitGroundingBlocked": frozenset({"unit_id", "issues"}),
    "UnitConfirmed": frozenset({"unit_id"}),
    "DocumentQualityChecked": frozenset(
        {"document_id", "confirmable", "issue_count"}
    ),
    "RevisionPlanApproved": frozenset({"document_id", "unit_ids"}),
    "PrdRendered": frozenset({"document_version"}),
    "PrdFinalized": frozenset({"document_id"}),
    "PrdReopened": frozenset({"unit_ids"}),
    "RunFailed": frozenset({"node"}),
    "ExportPreviewed": frozenset(
        {"intent_id", "mode", "document_version", "content_hash"}
    ),
    "ExportStarted": frozenset(
        {"export_run_id", "mode", "document_version"}
    ),
    "ExportSucceeded": frozenset(
        {"export_run_id", "mode", "document_version", "binding_id"}
    ),
    "ExportFailed": frozenset(
        {"export_run_id", "mode", "status", "error_code", "retryable"}
    ),
}


def public_event_payload(event_type: str, payload: dict) -> dict:
    allowed = PUBLIC_EVENT_PAYLOAD_FIELDS.get(event_type, frozenset())
    return {key: _value(payload[key]) for key in allowed if key in payload}


def _value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_value(item) for item in value]
    if isinstance(value, list):
        return [_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _value(item) for key, item in value.items()}
    return value


def display_status(task, run=None, outline=None) -> str:
    if task.status == TaskStatus.COMPLETED:
        return "COMPLETED"
    if task.status == TaskStatus.FAILED or (run and run.status == RunStatus.FAILED):
        return "FAILED"
    if task.status == TaskStatus.STOPPED or (run and run.status == RunStatus.STOPPED):
        return "STOPPED"
    if run and run.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
        return "IN_PROGRESS"
    if task.status == TaskStatus.CLARIFYING:
        return "NEEDS_INPUT"
    if task.status in {TaskStatus.OUTLINE_REVIEW, TaskStatus.FINAL_REVIEW}:
        return "AWAITING_CONFIRMATION"
    if outline and any(
        unit.status == UnitStatus.PENDING_CONFIRMATION
        for unit in outline.confirmation_units
    ):
        return "AWAITING_CONFIRMATION"
    return "IN_PROGRESS"


def task_summary(task, run=None, outline=None) -> TaskSummaryView:
    status = display_status(task, run, outline)
    return TaskSummaryView(
        task_id=task.task_id,
        title=task.title or "未命名 PRD",
        task_status=task.status.value,
        run_status=run.status.value if run else None,
        display_status=status,
        version=task.version,
        updated_at=task.updated_at,
        current_unit_sequence=task.current_unit_sequence,
        attention_required=status in {"NEEDS_INPUT", "AWAITING_CONFIRMATION", "FAILED"},
    )


def available_actions(snapshot) -> list[AvailableActionView]:
    task = snapshot.task
    outline = snapshot.current_outline
    actions: list[AvailableActionView] = []
    common = {"expected_task_version": task.version}
    if task.status == TaskStatus.CLARIFYING:
        actions.append(
            AvailableActionView(type="SEND_MESSAGE", label="补充需求", **common)
        )
    elif task.status == TaskStatus.OUTLINE_REVIEW and outline is not None:
        actions.extend(
            [
                AvailableActionView(type="SEND_MESSAGE", label="调整大纲", **common),
                AvailableActionView(
                    type="CONFIRM_OUTLINE",
                    label="确认大纲",
                    target_id=outline.outline_id,
                    payload={"outline_version": outline.version},
                    **common,
                ),
            ]
        )
    elif task.status == TaskStatus.GENERATING and outline is not None:
        current = next(
            (
                item
                for item in outline.confirmation_units
                if item.sequence == task.current_unit_sequence
            ),
            None,
        )
        if current and current.status == UnitStatus.PENDING_CONFIRMATION:
            actions.append(
                AvailableActionView(
                    type="CONFIRM_UNIT",
                    label="确认本单元",
                    target_id=current.unit_id,
                    **common,
                )
            )
    elif task.status == TaskStatus.FINAL_REVIEW and snapshot.documents:
        document = snapshot.documents[-1]
        quality = next(
            (
                item
                for item in reversed(snapshot.quality_results)
                if item.scope_id == document.document_id
            ),
            None,
        )
        blocking = (
            [
                issue
                for issue in quality.issues
                if issue.severity.value in {"BLOCKER", "ERROR"}
            ]
            if quality
            else []
        )
        if blocking:
            unit_ids = sorted(
                {
                    unit_id
                    for issue in blocking
                    for unit_id in issue.affected_unit_ids
                }
            )
            if unit_ids:
                actions.append(
                    AvailableActionView(
                        type="APPROVE_REVISION_PLAN",
                        label="批准修订",
                        target_id=document.document_id,
                        payload={
                            "document_id": document.document_id,
                            "issue_ids": [issue.issue_id for issue in blocking],
                            "unit_ids": unit_ids,
                        },
                        **common,
                    )
                )
        elif quality is None or quality.confirmable:
            actions.append(
                AvailableActionView(
                    type="FINALIZE_PRD",
                    label="最终确认",
                    target_id=document.document_id,
                    payload={
                        "document_id": document.document_id,
                        "content_hash": document.content_hash,
                    },
                    **common,
                )
            )
    elif task.status == TaskStatus.COMPLETED and outline is not None:
        actions.append(
            AvailableActionView(
                type="REOPEN_PRD",
                label="重新打开",
                payload={
                    "unit_ids": [
                        item.unit_id for item in outline.confirmation_units
                    ]
                },
                destructive=True,
                **common,
            )
        )
    return actions


_PUBLIC_LOCATOR_KEYS = frozenset(
    {
        "path",
        "line_start",
        "line_end",
        "symbol",
        "source_uri",
        "section_path",
        "chunk_id",
        "corpus_id",
        "corpus_version",
    }
)


def _investigation_views(records) -> list[InvestigationView]:
    content_policy = RepositoryContentPolicy()
    result = []
    for record in records:
        investigation = record["investigation"]
        need = record["need"]
        executions = record["executions"]
        evidence = [
            item
            for execution in executions
            for item in execution.bundle.evidence
        ]
        facts = [
            item for execution in executions for item in execution.bundle.facts
        ]
        unknowns = [
            item for execution in executions for item in execution.bundle.unknowns
        ]
        conflicts = [
            item for execution in executions for item in execution.bundle.conflicts
        ]
        evidence_views = []
        for item in evidence:
            excerpt, changed = content_policy.redact(item.excerpt)
            locator = {
                key: value
                for key, value in item.locator.items()
                if key in _PUBLIC_LOCATOR_KEYS
            }
            evidence_views.append(
                EvidenceView(
                    evidence_id=item.evidence_id,
                    source_kind=item.source_kind.value,
                    source_id=item.source_id,
                    source_version=item.source_version,
                    locator=locator,
                    excerpt=excerpt,
                    content_hash=item.content_hash,
                    extraction_method=item.extraction_method.value,
                    redaction_applied=item.redaction_applied or changed,
                )
            )
        result.append(
            InvestigationView(
                investigation_id=investigation.investigation_id,
                information_need_id=investigation.information_need_id,
                unit_id=investigation.unit_id,
                question=need.question,
                requiredness=need.requiredness.value,
                source_types=list(need.source_types),
                status=investigation.status.value,
                stop_reason=(
                    investigation.stop_reason.value
                    if investigation.stop_reason
                    else None
                ),
                coverage=[
                    CoverageItemView(
                        key=item.key,
                        description=item.description,
                        status=item.status.value,
                        evidence_ids=list(item.evidence_ids),
                        fact_ids=list(item.fact_ids),
                        unknown_ids=list(item.unknown_ids),
                        conflict_ids=list(item.conflict_ids),
                    )
                    for item in investigation.coverage.values()
                ],
                steps=[
                    InvestigationStepView(
                        sequence=item.sequence,
                        iteration=item.iteration,
                        step_type=item.step_type,
                        status=item.status,
                        public_summary=item.public_summary,
                    )
                    for item in record["steps"]
                ],
                tool_calls=[
                    ToolCallView(
                        tool_call_id=execution.tool_call.tool_call_id,
                        tool_id=execution.tool_call.tool_id,
                        status=execution.tool_call.status.value,
                        purpose=execution.tool_call.purpose,
                        public_summary=execution.tool_call.public_summary,
                        error_code=execution.tool_call.error_code,
                        started_at=execution.tool_call.started_at,
                        ended_at=execution.tool_call.ended_at,
                    )
                    for execution in executions
                ],
                evidence=evidence_views,
                facts=[
                    FactView(
                        fact_id=item.fact_id,
                        subject=item.subject,
                        predicate=item.predicate,
                        value=content_policy.redact_structure(item.value_json)[0],
                        fact_scope=item.fact_scope.value,
                        fact_type=item.fact_type.value,
                        verification_status=item.verification_status.value,
                        evidence_ids=list(item.evidence_ids),
                    )
                    for item in facts
                ],
                unknowns=[
                    UnknownView(
                        unknown_id=item.unknown_id,
                        statement=item.statement,
                        reason=item.reason.value,
                        severity=item.severity,
                        resolution_type=item.resolution_type,
                    )
                    for item in unknowns
                ],
                conflicts=[
                    ConflictView(
                        conflict_id=item.conflict_id,
                        subject=item.subject,
                        description=content_policy.redact(item.description)[0],
                        fact_ids=list(item.fact_ids),
                        status=item.status,
                    )
                    for item in conflicts
                ],
            )
        )
    return result


def task_detail(
    snapshot, latest_sequence: int, investigation_records=()
) -> TaskDetailResponse:
    outline = snapshot.current_outline
    outline_view = None
    if outline:
        outline_view = OutlineView(
            outline_id=outline.outline_id,
            version=outline.version,
            title=outline.title,
            status=outline.status.value,
            nodes=[
                OutlineNodeView(
                    node_id=item.node_id,
                    sequence=item.sequence,
                    title=item.title,
                    purpose=item.purpose,
                    complexity=item.complexity.value,
                    parent_id=item.parent_id,
                    level=item.level,
                )
                for item in outline.nodes
            ],
            units=[
                UnitView(
                    unit_id=item.unit_id,
                    sequence=item.sequence,
                    title=item.title,
                    status=item.status.value,
                    node_ids=list(item.node_ids),
                    depends_on_unit_ids=list(item.depends_on_unit_ids),
                    content=item.content,
                    content_hash=item.content_hash,
                )
                for item in outline.confirmation_units
            ],
        )
    document = snapshot.documents[-1] if snapshot.documents else None
    latest_grounding_by_unit = {}
    for item in snapshot.grounding_results:
        latest_grounding_by_unit[item.unit_id] = item
    public_quality = []
    if document is not None:
        matches = [
            item
            for item in snapshot.quality_results
            if item.scope_id == document.document_id
        ]
        if matches:
            public_quality = [matches[-1]]
    return TaskDetailResponse(
        task=task_summary(snapshot.task, snapshot.run, outline),
        run=RunView(
            run_id=snapshot.run.run_id,
            status=snapshot.run.status.value,
            started_at=snapshot.run.started_at,
            ended_at=snapshot.run.ended_at,
            error="执行失败，请使用请求 ID 查看日志。" if snapshot.run.error else None,
        ),
        brief=(
            _value(snapshot.current_brief.brief.as_dict())
            if snapshot.current_brief
            else None
        ),
        messages=[
            MessageView(
                message_id=item.message_id,
                role="user" if item.actor_id == snapshot.task.owner_id else "system",
                content=item.content,
                created_at=item.created_at,
            )
            for item in snapshot.messages
        ],
        outline=outline_view,
        investigations=_investigation_views(investigation_records),
        grounding=[
            GroundingView(
                grounding_run_id=item.grounding_run_id,
                unit_id=item.unit_id,
                status=item.status.value,
                confirmable=item.confirmable,
                issues=list(item.issues),
                reference_count=len(item.references),
            )
            for item in latest_grounding_by_unit.values()
        ],
        quality=[
            QualityView(
                quality_run_id=item.quality_run_id,
                scope=item.scope.value,
                scope_id=item.scope_id,
                confirmable=item.confirmable,
                issues=[
                    QualityIssueView(
                        issue_id=issue.issue_id,
                        issue_type=issue.issue_type.value,
                        severity=issue.severity.value,
                        description=issue.description,
                        suggested_resolution=issue.suggested_resolution,
                        affected_unit_ids=list(issue.affected_unit_ids),
                    )
                    for issue in item.issues
                ],
            )
            for item in public_quality
        ],
        document=(
            DocumentView(
                document_id=document.document_id,
                version=document.version,
                status=document.status.value,
                content_hash=document.content_hash,
                markdown=document.markdown,
                created_at=document.created_at,
            )
            if document
            else None
        ),
        available_actions=available_actions(snapshot),
        latest_event_sequence=latest_sequence,
    )


def encode_cursor(task) -> str:
    raw = json.dumps(
        [task.updated_at.isoformat(), task.task_id],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        timestamp, task_id = json.loads(
            base64.urlsafe_b64decode(cursor + padding).decode()
        )
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None or not task_id:
            raise ValueError
        return parsed, str(task_id)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidCommand("invalid task cursor") from exc


def task_list(repository, owner_id: str, cursor: str | None, limit: int) -> TaskListResponse:
    rows = repository.list_tasks(
        owner_id,
        before=decode_cursor(cursor),
        limit=limit + 1,
    )
    visible = rows[:limit]
    summaries = []
    for task in visible:
        try:
            snapshot = repository.snapshot_for_owner(task.task_id, owner_id)
            summaries.append(task_summary(task, snapshot.run, snapshot.current_outline))
        except NotFound:
            summaries.append(task_summary(task))
    return TaskListResponse(
        items=summaries,
        next_cursor=encode_cursor(visible[-1]) if len(rows) > limit else None,
    )
