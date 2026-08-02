from __future__ import annotations

import json

import pytest

from agent.bootstrap import DeterministicStructuredModel
from agent.checkpoint import CheckpointCodec
from agent.context import RunContext
from agent.graph import LangGraphAgentLoop
from agent.quality import DraftQualityPolicy
from agent.runtime import BufferedRuntimeEventSink
from agent.unit.models import RunPurpose, UnitScope
from agent.v1 import agent_execution_pb2 as proto


def _scope_proto(*, scope_hash: str | None = None) -> proto.UnitScope:
    scope = UnitScope(
        purpose=RunPurpose.GENERATE_UNIT,
        outline_id="outline-1",
        outline_version=1,
        outline_hash="a" * 64,
        current_unit_key="requirements",
        current_unit_title="需求与验收",
        current_unit_ordinal=1,
        section_node_keys=("requirements",),
    )
    return proto.UnitScope(
        schema_version=scope.schema_version,
        outline_id=scope.outline_id,
        outline_version=scope.outline_version,
        outline_hash=scope.outline_hash,
        current_unit_key=scope.current_unit_key,
        current_unit_title=scope.current_unit_title,
        current_unit_ordinal=scope.current_unit_ordinal,
        section_node_keys=scope.section_node_keys,
        scope_hash=scope_hash or scope.scope_hash,
    )


def _context(scope: proto.UnitScope) -> RunContext:
    return RunContext(
        run_id="run-unit",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task",
        task_message="新增可评审需求",
        workflow_version="agent-runtime.v4",
        checkpoint=b"",
        task_version=3,
        run_purpose=proto.RUN_PURPOSE_GENERATE_UNIT,
        unit_scope=scope,
    )


def test_langgraph_routes_scoped_v4_run_to_run_output_not_legacy_draft() -> None:
    result = LangGraphAgentLoop(
        model=DeterministicStructuredModel(),
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
    )(_context(_scope_proto()))

    assert result.draft_key is None
    assert result.run_output is not None
    assert result.run_output.output_kind == proto.RUN_OUTPUT_KIND_UNIT_CANDIDATE
    assert result.run_output.run_purpose == proto.RUN_PURPOSE_GENERATE_UNIT
    assert result.run_output.expected_task_version == 3
    payload = json.loads(result.run_output.payload)
    assert payload["unit_key"] == "requirements"
    assert payload["quality_report"]["schema_version"] == "quality-report.v2"


def test_langgraph_rejects_tampered_go_scope_hash_before_model_call() -> None:
    loop = LangGraphAgentLoop(
        model=DeterministicStructuredModel(),
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
    )

    with pytest.raises(ValueError, match="scope hash mismatch"):
        loop(_context(_scope_proto(scope_hash="b" * 64)))


def test_scoped_production_path_persists_claim_and_grounding_artifacts() -> None:
    sink = BufferedRuntimeEventSink()
    context = _context(_scope_proto())
    context = RunContext(**{**context.__dict__, "event_sink": sink})

    result = LangGraphAgentLoop(
        model=DeterministicStructuredModel(),
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
    )(context)

    assert result.run_output is not None
    assert [item.artifact_type for item in sink.artifacts] == [
        "UNIT_CLAIM_SET",
        "UNIT_GROUNDING_REPORT",
    ]
