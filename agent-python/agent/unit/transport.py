from __future__ import annotations

import hashlib
import json

from agent.result import AgentResult
from agent.unit.models import (
    ConfirmedUnitContext,
    RunOutputKind,
    RunPurpose,
    UnitRunResult,
    UnitScope,
)
from agent.v1 import agent_execution_pb2 as proto


_PURPOSE_TO_PROTO = {
    RunPurpose.PLAN_OUTLINE: proto.RUN_PURPOSE_PLAN_OUTLINE,
    RunPurpose.GENERATE_UNIT: proto.RUN_PURPOSE_GENERATE_UNIT,
    RunPurpose.REVISE_UNIT: proto.RUN_PURPOSE_REVISE_UNIT,
    RunPurpose.FULL_REVIEW: proto.RUN_PURPOSE_FULL_REVIEW,
}

_KIND_TO_PROTO = {
    RunOutputKind.OUTLINE_CANDIDATE: proto.RUN_OUTPUT_KIND_OUTLINE_CANDIDATE,
    RunOutputKind.UNIT_CANDIDATE: proto.RUN_OUTPUT_KIND_UNIT_CANDIDATE,
    RunOutputKind.UNIT_PATCH: proto.RUN_OUTPUT_KIND_UNIT_PATCH,
    RunOutputKind.FULL_REVIEW_REPORT: proto.RUN_OUTPUT_KIND_FULL_REVIEW_REPORT,
}

_PURPOSE_FROM_PROTO = {value: key for key, value in _PURPOSE_TO_PROTO.items()}


def decode_unit_scope(run_purpose: int, value: proto.UnitScope) -> UnitScope:
    try:
        purpose = _PURPOSE_FROM_PROTO[run_purpose]
    except KeyError as error:
        raise ValueError("unsupported run purpose") from error
    scope = UnitScope(
        purpose=purpose,
        schema_version=value.schema_version,
        outline_id=value.outline_id,
        outline_version=value.outline_version,
        outline_hash=value.outline_hash,
        current_unit_key=value.current_unit_key,
        current_unit_title=value.current_unit_title,
        current_unit_ordinal=value.current_unit_ordinal,
        section_node_keys=tuple(value.section_node_keys),
        dependency_unit_keys=tuple(value.dependency_unit_keys),
        confirmed_context=tuple(
            ConfirmedUnitContext(
                unit_key=item.unit_key,
                unit_version=item.unit_version,
                content_hash=item.content_hash,
                summary=item.summary,
                working_draft_ref=item.working_draft_ref,
                markdown=item.markdown,
            )
            for item in value.confirmed_context
        ),
        reopened_unit_keys=tuple(value.reopened_unit_keys),
        immutable_unit_keys=tuple(value.immutable_unit_keys),
        requirement_brief_ref=value.requirement_brief_ref,
        requirement_brief_hash=value.requirement_brief_hash,
        base_unit_hash=value.base_unit_hash,
        user_feedback=value.user_feedback,
    )
    supplied = value.scope_hash.removeprefix("sha256:")
    if not supplied or supplied != scope.scope_hash:
        raise ValueError("unit scope hash mismatch")
    return scope


def encode_run_output(
    result: UnitRunResult,
    *,
    output_key: str,
    expected_task_version: int,
) -> proto.RunOutput:
    """Encode a validated unit result into the cross-runtime submission contract."""
    if not output_key.strip() or expected_task_version < 1:
        raise ValueError("run output requires a key and expected task version")
    payload = json.dumps(
        result.payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    content_hash = hashlib.sha256(payload).hexdigest()
    if content_hash != result.content_hash.removeprefix("sha256:"):
        raise ValueError("run result content hash does not match canonical payload")
    return proto.RunOutput(
        schema_version="run-output.v1",
        output_key=output_key.strip(),
        output_kind=_KIND_TO_PROTO[result.output_kind],
        run_purpose=_PURPOSE_TO_PROTO[result.purpose],
        scope_hash=result.scope_hash.removeprefix("sha256:"),
        expected_task_version=expected_task_version,
        content_hash=content_hash,
        payload=payload,
    )


def to_agent_result(
    result: UnitRunResult,
    *,
    output_key: str,
    expected_task_version: int,
) -> AgentResult:
    return AgentResult(
        run_output=encode_run_output(
            result,
            output_key=output_key,
            expected_task_version=expected_task_version,
        )
    )
