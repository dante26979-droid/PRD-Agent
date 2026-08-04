from __future__ import annotations

from agent.context import RunContext
from agent.context_pack import ContextPolicy
from agent.graph.runtime import _context_trace_state
from agent.model_execution import ModelCallIntent, ModelExecutionModule
from agent.runtime import BufferedRuntimeEventSink, RunExecutionLedger
from agent.testing import ScriptedAgentModel
from agent.v1 import agent_execution_pb2 as proto


def _context(sink):
    return RunContext(
        run_id="run-model-context",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task-model-context",
        task_message="生成单元",
        workflow_version="agent-runtime.v4",
        checkpoint=b"",
        execution_ledger_version="run-ledger.v1",
        run_budget=proto.RunBudget(
            max_model_attempts=5,
            max_tool_calls=2,
            max_iterations=2,
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


def _intent():
    return ModelCallIntent(
        operation="generate_unit",
        operation_sequence=1,
        operation_key="model:generate_unit:sequence1",
        prompt_version="agent-runtime.generate_unit.v3",
        system_prompt="system",
        max_output_tokens=1_000,
    )


def test_model_execution_enforce_uses_context_pack_request_identity():
    sink = BufferedRuntimeEventSink()
    context = _context(sink)
    model = ScriptedAgentModel([{"markdown": "结果"}])
    policy = ContextPolicy(
        version="context-policy.v1",
        mode="enforce",
        context_window_tokens=5_000,
        compact_threshold_tokens=1_000,
        target_input_tokens=800,
        reserved_output_tokens=1_000,
        emergency_margin_tokens=300,
    )
    ledger = RunExecutionLedger(context)
    result = ModelExecutionModule(model, context_policy=policy).execute(
        context=context,
        sink=sink,
        ledger=ledger,
        intent=_intent(),
        payload={
            "unit_operation": "GENERATE_UNIT",
            "expected_schema": "unit-candidate.v1",
            "task_message": "生成单元",
            "unit_scope": {
                "current_unit_key": "u1",
                "dependency_unit_keys": [],
                "confirmed_context": [],
            },
            "output_contract": {"schema_version": "unit-candidate.v1"},
            "instruction": "return unit",
        },
    )

    assert result.response.output
    assert model.calls[0]["context_pack"]["policy_version"] == "context-policy.v1"
    entries = [event.entry for event in sink.ledger_events if event.event_type == "LEDGER_FINISHED"]
    assert [entry.entry_kind for entry in entries] == ["LOCAL_DERIVATION", "MODEL"]
    assert entries[-1].request_hash == result.prepared_context.request_hash
    trace = _context_trace_state(context, sink, list(ledger.entries()))
    assert trace["context_pack_id"] == result.prepared_context.context_pack.pack_id
    assert trace["context_policy_version"] == "context-policy.v1"
    assert trace["business_model_attempt_count"] == 1
    assert trace["context_compaction_attempt_count"] == 0


def test_model_execution_off_preserves_original_payload_and_skips_pack_artifact():
    sink = BufferedRuntimeEventSink()
    context = _context(sink)
    model = ScriptedAgentModel([{"markdown": "结果"}])
    payload = {"task_message": "生成单元", "instruction": "return unit"}
    result = ModelExecutionModule(model).execute(
        context=context,
        sink=sink,
        ledger=RunExecutionLedger(context),
        intent=_intent(),
        payload=payload,
    )

    assert model.calls == [payload]
    assert result.prepared_context.context_pack is None
    assert [item.artifact_type for item in sink.artifacts] == [
        "MODEL_VALIDATED_OUTPUT"
    ]


def test_semantic_compaction_is_a_separate_budgeted_replayable_model_operation():
    sink = BufferedRuntimeEventSink()
    context = _context(sink)
    mandatory = {
        "unit_operation": "GENERATE_UNIT",
        "expected_schema": "unit-candidate.v1",
        "task_message": "生成单元",
        "unit_scope": {
            "current_unit_key": "u1",
            "dependency_unit_keys": [],
            "confirmed_context": [],
        },
        "output_contract": {"schema_version": "unit-candidate.v1"},
        "instruction": "return unit",
    }
    model = ScriptedAgentModel(
        [
            {"operation_view": mandatory, "_tokens": 20},
            {"markdown": "压缩后结果", "_tokens": 30},
        ]
    )
    policy = ContextPolicy(
        version="context-policy.v1-semantic",
        mode="enforce",
        context_window_tokens=8_000,
        compact_threshold_tokens=1_500,
        target_input_tokens=1_200,
        reserved_output_tokens=1_000,
        emergency_margin_tokens=500,
        semantic_compaction_enabled=True,
        semantic_max_output_tokens=500,
        max_optional_string_chars=8_000,
    )
    result = ModelExecutionModule(model, context_policy=policy).execute(
        context=context,
        sink=sink,
        ledger=RunExecutionLedger(context),
        intent=_intent(),
        payload={**mandatory, "notes": "过程信息" * 3_000},
    )

    assert result.prepared_context.context_pack.compaction_kind == "SEMANTIC"
    assert len(model.calls) == 2
    assert model.calls[0]["schema_version"] == "context-compaction-request.v1"
    assert "notes" not in model.calls[1]
    terminal = [
        event.entry
        for event in sink.ledger_events
        if event.event_type == "LEDGER_FINISHED"
    ]
    assert [(item.entry_kind, item.operation) for item in terminal] == [
        ("MODEL", "compact_context"),
        ("LOCAL_DERIVATION", "build_context_pack"),
        ("MODEL", "generate_unit"),
    ]


def test_semantic_compaction_preserves_budget_for_business_call():
    sink = BufferedRuntimeEventSink()
    context = _context(sink)
    context.run_budget.max_input_tokens = 1_000
    model = ScriptedAgentModel([{"markdown": "确定性降级结果"}])
    policy = ContextPolicy(
        version="context-policy.v1-semantic",
        mode="enforce",
        context_window_tokens=8_000,
        compact_threshold_tokens=1_500,
        target_input_tokens=1_200,
        reserved_output_tokens=1_000,
        emergency_margin_tokens=500,
        semantic_compaction_enabled=True,
        semantic_max_output_tokens=500,
        max_optional_string_chars=8_000,
    )
    result = ModelExecutionModule(model, context_policy=policy).execute(
        context=context,
        sink=sink,
        ledger=RunExecutionLedger(context),
        intent=_intent(),
        payload={
            "unit_operation": "GENERATE_UNIT",
            "expected_schema": "unit-candidate.v1",
            "task_message": "生成单元",
            "unit_scope": {
                "current_unit_key": "u1",
                "dependency_unit_keys": [],
                "confirmed_context": [],
            },
            "output_contract": {"schema_version": "unit-candidate.v1"},
            "instruction": "return unit",
            "notes": "过程信息" * 3_000,
        },
    )

    assert len(model.calls) == 1
    assert result.prepared_context.context_pack.compaction_kind == (
        "DETERMINISTIC_EMERGENCY"
    )
