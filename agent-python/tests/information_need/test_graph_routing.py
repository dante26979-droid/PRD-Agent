import json
import hashlib
from dataclasses import replace

import pytest

from agent.checkpoint import CheckpointCodec
from agent.capability import RepositorySearchHit
from agent.context import RunContext
from agent.graph import LangGraphAgentLoop
from agent.investigation import InvestigationBudget
from agent.quality import DraftQualityPolicy
from agent.runtime import BufferedRuntimeEventSink
from agent.testing import InstrumentedCapabilityGateway, ScriptedAgentModel
from agent.v1 import agent_execution_pb2 as proto


def _context(sink, *, task_message, binding_id="", revision=""):
    return RunContext(
        run_id="run-need-graph",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task-need-graph",
        task_message=task_message,
        workflow_version="agent-runtime.v4",
        checkpoint=b"",
        task_version=1,
        repository_binding_id=binding_id,
        repository_revision=revision,
        execution_ledger_version="run-ledger.v1",
        run_budget=proto.RunBudget(
            max_model_attempts=5,
            max_tool_calls=3,
            max_iterations=3,
            max_replans=1,
            max_supplements=1,
            max_quality_repairs=1,
            max_input_tokens=50_000,
            max_output_tokens=10_000,
            max_elapsed_ms=60_000,
        ),
        consumed_budget=proto.ConsumedBudget(),
        event_sink=sink,
    )


def test_v4_none_need_persists_plan_and_skips_capability():
    sink = BufferedRuntimeEventSink()
    gateway = InstrumentedCapabilityGateway()
    model = ScriptedAgentModel(
        [
            {
                "question": "是否需要调查当前实现？",
                "suggested_requiredness": "NONE",
                "need_kind": "NEW_BEHAVIOR",
                "source_types": [],
                "fallback": "按目标态生成",
            },
            {"markdown": "# PRD\n\n新增独立欢迎页。"},
        ]
    )

    result = LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda _context: gateway,
    )(
        _context(
            sink,
            task_message="新增一个独立欢迎页，不依赖当前系统实现",
        )
    )

    payload = json.loads(result.draft_patch)
    statuses = [
        CheckpointCodec().decode(item).payload["status"]
        for _, item in sink.checkpoints
    ]
    assert gateway.observations == []
    assert payload["coverage"] == {}
    assert "NEED_PLANNED" in statuses
    assert [item.artifact_type for item in sink.artifacts].count(
        "INFORMATION_NEED_PLAN"
    ) == 1


def test_v4_required_need_rejects_direct_markdown_before_investigation():
    sink = BufferedRuntimeEventSink()
    model = ScriptedAgentModel(
        [
            {
                "question": "当前订单查询 API 如何实现？",
                "suggested_requiredness": "NONE",
                "need_kind": "FIELD_OR_FORMAT_CHANGE",
                "source_types": [],
                "fallback": "当前态标记为 Unknown",
            },
            {"markdown": "# PRD\n\n未经调查的当前态断言。"},
        ]
    )

    with pytest.raises(ValueError, match="REQUIRED_NEED_UNSATISFIED"):
        LangGraphAgentLoop(
            model=model,
            checkpoint_codec=CheckpointCodec(),
            quality_policy=DraftQualityPolicy(),
            capability_factory=lambda _context: InstrumentedCapabilityGateway(),
        )(
            _context(
                sink,
                task_message="修改订单查询 API，新增 created_at 筛选字段",
                binding_id="binding-1",
                revision="a" * 40,
            )
        )


def test_v4_required_need_enters_the_capability_loop():
    sink = BufferedRuntimeEventSink()
    gateway = InstrumentedCapabilityGateway(
        repository_hits={
            "order api": (
                RepositorySearchHit("api/orders.py", 10, "def list_orders"),
            )
        }
    )
    model = ScriptedAgentModel(
        [
            {
                "question": "当前订单查询 API 如何实现？",
                "suggested_requiredness": "NONE",
                "need_kind": "FIELD_OR_FORMAT_CHANGE",
                "source_types": [],
                "fallback": "当前态标记为 Unknown",
            },
            {
                "action": {
                    "tool_id": "search_repository",
                    "tool_schema_version": "1",
                    "arguments": {"query": "order api"},
                    "purpose": "确认接口字段",
                    "target_coverage": ["api_contract"],
                }
            },
            {"markdown": "# PRD\n\n其余当前实现保留为 Unknown。"},
        ]
    )

    result = LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda _context: gateway,
        budget=InvestigationBudget(max_iterations=1, max_tool_calls=1),
    )(
        _context(
            sink,
            task_message="修改订单查询 API，新增 created_at 筛选字段",
            binding_id="binding-1",
            revision="a" * 40,
        )
    )

    payload = json.loads(result.draft_patch)
    assert len(gateway.observations) == 1
    assert payload["coverage"]["api_contract"] == "MISSING"
    assert any(
        item.artifact_type == "KNOWLEDGE_BUNDLE" for item in sink.artifacts
    )
    assert payload["coverage"]["validation_logic"] == "MISSING"
    assert payload["result_outcome"] == "PARTIAL_EVIDENCE"


def test_v4_required_need_without_source_stops_with_explicit_human_input_reason():
    sink = BufferedRuntimeEventSink()
    model = ScriptedAgentModel(
        [
            {
                "question": "当前订单查询 API 如何实现？",
                "suggested_requiredness": "REQUIRED",
                "need_kind": "FIELD_OR_FORMAT_CHANGE",
                "source_types": ["CODE"],
                "fallback": "当前态标记为 Unknown",
            },
            {"markdown": "# PRD\n\n代码来源不可用，当前态为 Unknown。"},
        ]
    )

    result = LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
    )(
        _context(
            sink,
            task_message="修改订单查询 API，新增 created_at 筛选字段",
        )
    )

    payload = json.loads(result.draft_patch)
    need_checkpoint = next(
        CheckpointCodec().decode(item).payload["snapshot"]
        for _, item in sink.checkpoints
        if CheckpointCodec().decode(item).payload["status"] == "NEED_PLANNED"
    )
    assert need_checkpoint["need_route"] == "PAUSE_FOR_HUMAN"
    assert need_checkpoint["need_route_reason_code"] == "REQUIRED_SOURCE_UNAVAILABLE"
    assert payload["stop_reason"] == "HUMAN_INPUT_REQUIRED"


def test_v4_need_planned_resume_does_not_reinvoke_the_planner():
    first_sink = BufferedRuntimeEventSink()
    first_model = ScriptedAgentModel(
        [
            {
                "question": "是否需要调查当前实现？",
                "suggested_requiredness": "NONE",
                "need_kind": "NEW_BEHAVIOR",
                "source_types": [],
                "fallback": "按目标态生成",
            },
            {"markdown": "# PRD\n\n首次草稿。"},
        ]
    )
    first_context = _context(
        first_sink,
        task_message="新增一个独立欢迎页，不依赖当前系统实现",
    )
    LangGraphAgentLoop(
        model=first_model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
    )(first_context)
    need_sequence, need_checkpoint = next(
        (sequence, item)
        for sequence, item in first_sink.checkpoints
        if CheckpointCodec().decode(item).payload["status"] == "NEED_PLANNED"
    )
    planner_entry = next(
        event.entry
        for event in reversed(first_sink.ledger_events)
        if event.entry.operation == "plan_information_need"
        and event.entry.status == "SUCCEEDED"
    )
    durable_artifacts = tuple(
        item
        for item in first_sink.artifacts
        if item.artifact_type == "INFORMATION_NEED_PLAN"
        or item.artifact_key == planner_entry.output_artifact_key
    )
    resumed_sink = BufferedRuntimeEventSink()
    resumed_context = replace(
        first_context,
        checkpoint=need_checkpoint,
        checkpoint_sequence=need_sequence,
        resume_artifacts=durable_artifacts,
        ledger_entries=(planner_entry,),
        consumed_budget=proto.ConsumedBudget().FromString(
            planner_entry.consumption.SerializeToString()
        ),
        resume_summary=proto.ResumeStateSummary(
            checkpoint_content_hash="sha256:"
            + hashlib.sha256(need_checkpoint).hexdigest(),
            # Ledger-owned model calls are authoritative for v4; the legacy
            # model-attempt summary may remain zero.
            terminal_model_attempt_count=0,
            artifact_count=len(durable_artifacts),
            artifacts=(
                proto.RunArtifactIdentity(
                    artifact_key=item.artifact_key,
                    artifact_type=item.artifact_type,
                    generation=item.generation,
                    request_hash=item.request_hash,
                    content_hash=item.content_hash,
                )
                for item in durable_artifacts
            ),
        ),
        event_sink=resumed_sink,
    )
    resumed_model = ScriptedAgentModel(
        [{"markdown": "# PRD\n\n恢复后的草稿。"}]
    )

    LangGraphAgentLoop(
        model=resumed_model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
    )(resumed_context)

    assert len(resumed_model.calls) == 1
    assert "allowed_need_kinds" not in resumed_model.calls[0]
    assert all(
        item.artifact_type != "INFORMATION_NEED_PLAN"
        for item in resumed_sink.artifacts
    )
