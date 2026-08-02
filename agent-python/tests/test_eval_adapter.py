from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from agent.capability import RepositorySearchHit
from agent.checkpoint import CheckpointCodec
from agent.context import RunContext
from agent.eval_adapter import TraceRuntimeEventSink, TraceSequenceError, build_trace_payload
from agent.graph import LangGraphAgentLoop
from agent.graph.snapshot import LoopCheckpointStatus, LoopSnapshot
from agent.investigation import InvestigationBudget
from agent.quality import DraftQualityPolicy
from agent.testing import InstrumentedCapabilityGateway, InstrumentedModel, ScriptedAgentModel
from agent.v1 import agent_execution_pb2 as proto


def _context(sink):
    return RunContext(
        run_id="eval-run-case-006-1",
        tenant_id="eval-tenant",
        owner_id="eval-owner",
        task_id="case-006",
        task_message="生成订单取消权限 PRD",
        workflow_version="agent-runtime.v1",
        checkpoint=b"",
        repository_binding_id="demo-repo",
        repository_revision="a" * 40,
        event_sink=sink,
    )


def _build(sink, gateway, result, **changes):
    values = {
        "sink": sink,
        "capability_observations": gateway.observations,
        "result": result,
        "eval_run_id": "eval-run-case-006-1",
        "case_id": "case-006",
        "workflow_version": "agent-runtime.v1",
        "execution_mode": "scripted_characterization",
        "deterministic_only": True,
        "started_at": datetime(2026, 7, 31, tzinfo=timezone.utc),
        "duration_ms": 10,
        "input_hash": "sha256:" + "a" * 64,
    }
    values.update(changes)
    return build_trace_payload(**values)


def test_adapter_folds_real_loop_events_without_content():
    secret = "EVIDENCE_SECRET_MARKER_31aa"
    model = InstrumentedModel(
        ScriptedAgentModel(
            [
                {
                    "action": {
                        "tool_id": "search_repository",
                        "tool_schema_version": "1",
                        "arguments": {"query": "order permission"},
                        "purpose": "investigate permission",
                        "target_coverage": ["repository_evidence"],
                    },
                    "_tokens": 2,
                },
                {"markdown": "# PRD\n\n订单取消需要权限。", "_tokens": 3},
            ]
        )
    )
    gateway = InstrumentedCapabilityGateway(
        repository_hits={
            "order permission": (
                RepositorySearchHit("src/auth.py", 8, secret),
            )
        }
    )
    sink = TraceRuntimeEventSink()
    result = LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda _context: gateway,
        budget=InvestigationBudget(max_iterations=3, max_tool_calls=3),
    )(_context(sink))

    trace = _build(sink, gateway, result)
    encoded = json.dumps(trace, ensure_ascii=False)

    assert trace["counters"]["model_attempt_count"] == 2
    assert trace["counters"]["capability_physical_call_count"] == 1
    assert trace["counters"]["evidence_count"] == 1
    assert trace["counters"]["total_tokens"] == 5
    assert trace["coverage_transitions"][0]["before"] == "MISSING"
    assert trace["coverage_transitions"][0]["after"] == "COVERED"
    assert trace["coverage_transitions"][0]["fact_delta"] is None
    assert [item["sequence"] for item in trace["checkpoints"]] == [1, 2, 3, 4]
    assert secret not in encoded
    assert "订单取消需要权限" not in encoded
    assert "order permission" not in encoded


def test_terminal_attempt_without_planned_event_is_rejected():
    sink = TraceRuntimeEventSink()
    sink.model_attempt(
        proto.RecordModelAttemptRequest(
            attempt_key="attempt-1",
            operation="plan",
            prompt_version="v1",
            provider="scripted",
            request_hash="sha256:a",
            status="SUCCEEDED",
        )
    )
    gateway = InstrumentedCapabilityGateway()

    with pytest.raises(TraceSequenceError, match="no PLANNED"):
        _build(sink, gateway, None)


def test_adapter_exposes_only_low_cardinality_information_need_fields():
    sink = TraceRuntimeEventSink()
    state = {
        "run_id": "eval-run-case-006-1",
        "task_id": "case-006",
        "task_version": 1,
        "checkpoint_sequence": 1,
        "effective_requiredness": "REQUIRED",
        "information_need_kind": "PERMISSION_CHANGE",
        "information_need_policy_version": "information-need-policy.v1",
        "need_route": "EXECUTE_INVESTIGATION",
        "need_route_reason_code": "REQUIRED_SOURCE_AVAILABLE",
        "coverage": {"authorization_rule": "MISSING"},
    }
    payload = CheckpointCodec().encode(
        workflow_version="agent-runtime.v4",
        run_id="eval-run-case-006-1",
        task_version=1,
        sequence=1,
        payload=LoopSnapshot(
            LoopCheckpointStatus.INITIALIZED, state
        ).as_payload(workflow_version="agent-runtime.v4"),
    )
    sink.checkpoint(1, payload)

    trace = _build(
        sink,
        InstrumentedCapabilityGateway(),
        None,
        workflow_version="agent-runtime.v4",
    )

    assert trace["information_need"] == {
        "requiredness": "REQUIRED",
        "need_kind": "PERMISSION_CHANGE",
        "route": "EXECUTE_INVESTIGATION",
        "reason_code": "REQUIRED_SOURCE_AVAILABLE",
        "policy_version": "information-need-policy.v1",
        "coverage_count": 1,
    }


def test_v4_coverage_transition_reports_knowledge_deltas() -> None:
    sink = TraceRuntimeEventSink()
    codec = CheckpointCodec()
    for sequence, status, coverage, facts, unknowns in (
        (1, LoopCheckpointStatus.NEED_PLANNED, "MISSING", 0, 0),
        (2, LoopCheckpointStatus.OBSERVED, "COVERED", 1, 0),
    ):
        state = {
            "run_id": "eval-run-case-006-1",
            "task_id": "case-006",
            "task_version": 1,
            "checkpoint_sequence": sequence,
            "coverage": {"repository_structure": coverage},
            "knowledge_fact_count": facts,
            "knowledge_unknown_count": unknowns,
            "knowledge_conflict_count": 0,
        }
        sink.checkpoint(
            sequence,
            codec.encode(
                workflow_version="agent-runtime.v4",
                run_id="eval-run-case-006-1",
                task_version=1,
                sequence=sequence,
                payload=LoopSnapshot(status, state).as_payload(
                    workflow_version="agent-runtime.v4"
                ),
            ),
        )

    trace = _build(
        sink,
        InstrumentedCapabilityGateway(),
        None,
        workflow_version="agent-runtime.v4",
    )

    assert trace["coverage_transitions"] == [
        {
            "iteration": 0,
            "coverage_key": "repository_structure",
            "before": "MISSING",
            "after": "COVERED",
            "evidence_delta": 0,
            "fact_delta": 1,
            "unknown_delta": 0,
            "conflict_delta": 0,
            "trigger": "OBSERVED",
        }
    ]


def test_failed_attempt_keeps_category_not_raw_error():
    sink = TraceRuntimeEventSink()
    for status in ("PLANNED", "FAILED"):
        sink.model_attempt(
            proto.RecordModelAttemptRequest(
                attempt_key="attempt-1",
                operation="plan",
                prompt_version="v1",
                provider="scripted",
                request_hash="sha256:a",
                status=status,
                error_category="MODEL_TIMEOUT" if status == "FAILED" else "",
            )
        )
    gateway = InstrumentedCapabilityGateway()

    trace = _build(
        sink,
        gateway,
        None,
        failure_category="MODEL_TIMEOUT",
        failure_retryable=True,
    )

    assert trace["model_attempts"][0]["error_category"] == "MODEL_TIMEOUT"
    assert trace["failure"] == {
        "category": "MODEL_TIMEOUT",
        "retryable": True,
        "public_code": "MODEL_TIMEOUT",
    }


def test_instrumented_gateway_distinguishes_empty_and_physical_call():
    gateway = InstrumentedCapabilityGateway(repository_hits={"missing": ()})

    assert gateway.search_repository(query="missing", limit=10) == ()

    observation = gateway.observations[0]
    assert observation.status == "EMPTY"
    assert observation.physical_call is True
    assert observation.evidence_count == 0
    assert "missing" not in repr(observation)


def test_checkpoint_gap_is_rejected():
    codec = CheckpointCodec()
    sink = TraceRuntimeEventSink()
    for sequence in (1, 3):
        payload = codec.encode(
            workflow_version="agent-runtime.v1",
            run_id="eval-run-case-006-1",
            task_version=1,
            sequence=sequence,
            payload={
                "snapshot_schema_version": "agent-loop-snapshot.v2",
                "status": "OBSERVED",
                "snapshot": {"coverage": {"repository_evidence": "MISSING"}},
            },
        )
        sink.checkpoint(sequence, payload)

    with pytest.raises(TraceSequenceError, match="gap"):
        _build(sink, InstrumentedCapabilityGateway(), None)


def test_scripted_model_does_not_mutate_fixture():
    fixture = {"markdown": "# PRD", "_tokens": 4}
    model = ScriptedAgentModel([fixture])

    response = model.complete("system", "{}")

    assert response.token_usage["total_tokens"] == 4
    assert fixture == {"markdown": "# PRD", "_tokens": 4}


def test_artifact_trace_keeps_only_type_hashes_generation_and_size():
    sink = TraceRuntimeEventSink()
    sink.artifact(
        proto.RunArtifact(
            artifact_key="private-artifact-key",
            artifact_type="DRAFT_BUNDLE",
            generation=2,
            request_hash="sha256:request",
            content_hash="sha256:content",
            content=b"DRAFT_SECRET_MARKER_9d2f",
        )
    )

    trace = _build(sink, InstrumentedCapabilityGateway(), None)
    encoded = json.dumps(trace)

    assert trace["artifacts"] == [
        {
            "artifact_type": "DRAFT_BUNDLE",
            "generation": 2,
            "request_hash": "sha256:request",
            "content_hash": "sha256:content",
            "payload_bytes": len(b"DRAFT_SECRET_MARKER_9d2f"),
        }
    ]
    assert "private-artifact-key" not in encoded
    assert "DRAFT_SECRET_MARKER_9d2f" not in encoded
