from __future__ import annotations

import hashlib
import json

import pytest

from agent.context import RunContext
from agent.context_pack import (
    ContextCompactionOutputInvalid,
    ContextPackModule,
    ContextPolicy,
    ContextWindowUnsatisfiable,
    decode_context_pack_artifact,
)
from agent.runtime import BufferedRuntimeEventSink, RunExecutionLedger
from agent.runtime.idempotency import canonical_json
from agent.v1 import agent_execution_pb2 as proto


def _context(sink: BufferedRuntimeEventSink) -> RunContext:
    return RunContext(
        run_id="run-context",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task-context",
        task_message="生成支付确认单元",
        workflow_version="agent-runtime.v4",
        checkpoint=b"",
        execution_ledger_version="run-ledger.v1",
        run_budget=proto.RunBudget(
            max_model_attempts=4,
            max_tool_calls=2,
            max_iterations=3,
            max_replans=1,
            max_supplements=1,
            max_quality_repairs=1,
            max_input_tokens=100_000,
            max_output_tokens=20_000,
            max_elapsed_ms=60_000,
        ),
        consumed_budget=proto.ConsumedBudget(),
        event_sink=sink,
    )


def _policy(mode="enforce", **overrides) -> ContextPolicy:
    values = {
        "version": "context-policy.v1",
        "mode": mode,
        "context_window_tokens": 3_000,
        "compact_threshold_tokens": 900,
        "target_input_tokens": 650,
        "reserved_output_tokens": 500,
        "emergency_margin_tokens": 200,
        "max_optional_string_chars": 200,
    }
    values.update(overrides)
    return ContextPolicy(**values)


def _payload() -> dict[str, object]:
    return {
        "unit_operation": "GENERATE_UNIT",
        "expected_schema": "unit-candidate.v1",
        "task_message": "生成支付确认单元",
        "unit_scope": {
            "current_unit_key": "payment",
            "dependency_unit_keys": ["background"],
            "immutable_unit_keys": ["background"],
            "reopened_unit_keys": [],
            "confirmed_context": [
                {
                    "unit_key": "background",
                    "unit_version": 2,
                    "content_hash": "sha256:background",
                    "summary": "背景约束",
                    "working_draft_ref": "draft:background",
                    "markdown": "依赖正文" * 300,
                },
                {
                    "unit_key": "unrelated",
                    "unit_version": 1,
                    "content_hash": "sha256:unrelated",
                    "summary": "无关单元摘要",
                    "working_draft_ref": "draft:unrelated",
                    "markdown": "无关正文" * 300,
                },
            ],
        },
        "base_candidate": None,
        "output_contract": {"schema_version": "unit-candidate.v1"},
        "instruction": "Return only the current unit.",
    }


def test_enforce_builds_ledger_owned_pack_and_externalizes_non_dependency_body():
    sink = BufferedRuntimeEventSink()
    ledger = RunExecutionLedger(_context(sink))
    prepared = ContextPackModule(_policy()).prepare(
        context=_context(sink),
        ledger=ledger,
        operation="generate_unit",
        operation_sequence=1,
        prompt_version="agent-runtime.generate-unit.v1",
        system_prompt="system",
        payload=_payload(),
    )

    assert prepared.context_pack is not None
    assert prepared.context_pack.compaction_kind.startswith("DETERMINISTIC")
    assert prepared.payload["context_pack"]["policy_version"] == "context-policy.v1"
    confirmed = prepared.payload["unit_scope"]["confirmed_context"]
    assert confirmed[0]["markdown"].startswith("依赖正文")
    assert confirmed[1]["markdown"] == ""
    artifact = next(item for item in sink.artifacts if item.artifact_type == "CONTEXT_PACK")
    decoded = decode_context_pack_artifact(artifact)
    assert decoded["pack_id"] == prepared.context_pack.pack_id
    assert decoded["prompt_version"] == "agent-runtime.generate-unit.v1"
    assert decoded["output_schema"] == "model-response.v1"
    assert sink.ledger_events[-1].entry.entry_kind == "LOCAL_DERIVATION"


def test_shadow_persists_projection_but_keeps_authoritative_payload_unchanged():
    sink = BufferedRuntimeEventSink()
    original = _payload()
    prepared = ContextPackModule(_policy("shadow")).prepare(
        context=_context(sink),
        ledger=RunExecutionLedger(_context(sink)),
        operation="generate_unit",
        operation_sequence=1,
        system_prompt="system",
        payload=original,
    )

    assert prepared.payload == original
    assert "context_pack" not in prepared.payload
    assert prepared.shadow_payload is not None
    assert prepared.shadow_payload["unit_scope"]["confirmed_context"][1]["markdown"] == ""


def test_payload_below_threshold_is_not_compacted_to_the_lower_target():
    sink = BufferedRuntimeEventSink()
    payload = {
        "task_message": "需求",
        "notes": "仍在安全阈值内" * 100,
    }
    prepared = ContextPackModule(
        _policy(
            compact_threshold_tokens=2_000,
            target_input_tokens=100,
            max_optional_string_chars=10_000,
        )
    ).prepare(
        context=_context(sink),
        ledger=RunExecutionLedger(_context(sink)),
        operation="generate_working_draft",
        operation_sequence=1,
        system_prompt="system",
        payload=payload,
    )

    assert prepared.context_pack.compaction_kind == "NONE"
    assert prepared.context_pack.operation_view == payload


def test_mandatory_context_larger_than_hard_window_fails_closed():
    sink = BufferedRuntimeEventSink()
    payload = _payload()
    payload["task_message"] = "必须保留" * 4_000
    with pytest.raises(ContextWindowUnsatisfiable):
        ContextPackModule(
            _policy(
                context_window_tokens=1_000,
                compact_threshold_tokens=400,
                target_input_tokens=250,
                reserved_output_tokens=300,
                emergency_margin_tokens=100,
            )
        ).prepare(
            context=_context(sink),
            ledger=RunExecutionLedger(_context(sink)),
            operation="generate_unit",
            operation_sequence=1,
            system_prompt="system",
            payload=payload,
        )
    assert sink.artifacts == []


def test_semantic_view_cannot_change_mandatory_scope_or_create_ids():
    original = {
        "task_message": "需求",
        "unit_scope": {"current_unit_key": "unit-1"},
        "facts": [{"fact_id": "fact-1"}],
    }
    with pytest.raises(ContextCompactionOutputInvalid):
        ContextPackModule.validate_semantic_view(
            {
                "operation_view": {
                    "task_message": "需求",
                    "unit_scope": {"current_unit_key": "unit-2"},
                    "facts": [{"fact_id": "fact-new"}],
                }
            },
            original=original,
            mandatory_keys=frozenset({"task_message", "unit_scope"}),
        )


def test_semantic_view_cannot_rewrite_optional_content_without_provenance():
    with pytest.raises(ContextCompactionOutputInvalid, match="NEW_CONTENT"):
        ContextPackModule.validate_semantic_view(
            {
                "operation_view": {
                    "task_message": "需求",
                    "notes": "模型新写的、无法验证来源的摘要",
                }
            },
            original={"task_message": "需求", "notes": "原始支持材料"},
            mandatory_keys=frozenset({"task_message"}),
        )


def test_context_pack_payload_is_canonical_json():
    sink = BufferedRuntimeEventSink()
    prepared = ContextPackModule(_policy()).prepare(
        context=_context(sink),
        ledger=RunExecutionLedger(_context(sink)),
        operation="generate_unit",
        operation_sequence=1,
        system_prompt="system",
        payload=_payload(),
    )
    decoded = json.loads(prepared.canonical_payload)
    assert decoded["context_pack"]["pack_id"] == prepared.context_pack.pack_id


def test_context_pack_rejects_tampered_source_manifest_hash():
    sink = BufferedRuntimeEventSink()
    ContextPackModule(_policy()).prepare(
        context=_context(sink),
        ledger=RunExecutionLedger(_context(sink)),
        operation="generate_unit",
        operation_sequence=1,
        system_prompt="system",
        payload=_payload(),
    )
    artifact = next(item for item in sink.artifacts if item.artifact_type == "CONTEXT_PACK")
    tampered = proto.RunArtifact()
    tampered.CopyFrom(artifact)
    envelope = json.loads(tampered.content)
    envelope["value"]["source_manifest"]["manifest_hash"] = "sha256:" + "0" * 64
    tampered.content = canonical_json(envelope)
    tampered.content_hash = hashlib.sha256(tampered.content).hexdigest()

    with pytest.raises(ValueError, match="manifest hash mismatch"):
        decode_context_pack_artifact(tampered)
