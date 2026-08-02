from __future__ import annotations

import pytest

from agent.runtime import (
    DeterministicShadowEvaluator,
    NoRemoteEffectsAdapter,
    ShadowArtifactModule,
    ShadowCounters,
    ShadowRemoteEffectError,
)
from agent.context import RunContext
from agent.result import AgentResult
from agent.v1 import agent_execution_pb2 as proto


def test_no_remote_effects_adapter_fails_closed_for_any_operation() -> None:
    adapter = NoRemoteEffectsAdapter("MODEL")

    with pytest.raises(ShadowRemoteEffectError, match="MODEL:complete"):
        adapter.complete("system", "user")


def test_shadow_evaluator_accepts_deterministic_checks_with_zero_delta() -> None:
    baseline = ShadowCounters(model_physical_calls=1, capability_physical_calls=2)
    result = DeterministicShadowEvaluator().evaluate(
        authoritative_trace_hash="sha256:trace",
        candidate_policy_version="policy-v1",
        baseline_counters=baseline,
        observed_counters=baseline,
        checks=(lambda: {"code": "OUTLINE_SCOPE_OK"},),
        projected_need_requiredness="REQUIRED",
        projected_quality_codes=("B", "A", "A"),
    )

    assert result.extra_model_physical_calls == 0
    assert result.extra_capability_physical_calls == 0
    assert result.projected_quality_codes == ("A", "B")


def test_shadow_evaluator_rejects_additional_remote_call_or_write() -> None:
    with pytest.raises(ShadowRemoteEffectError, match="FORBIDDEN"):
        DeterministicShadowEvaluator().evaluate(
            authoritative_trace_hash="trace",
            candidate_policy_version="policy-v1",
            baseline_counters=ShadowCounters(),
            observed_counters=ShadowCounters(unit_writes=1),
        )


def test_shadow_artifact_is_hash_bound_and_replay_stable() -> None:
    context = RunContext(
        run_id="run-shadow",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task",
        task_message="message",
        workflow_version="agent-runtime.v1",
        checkpoint=b"",
        evaluation_mode="SHADOW",
        authoritative_workflow_version="agent-runtime.v1",
        shadow_workflow_version="agent-runtime.v4",
        candidate_policy_version="policy-v4",
        assignment_hash="sha256:assignment",
    )
    result = AgentResult(
        run_output=proto.RunOutput(
            schema_version="run-output.v1",
            output_key="output-1",
            output_kind=proto.RUN_OUTPUT_KIND_OUTLINE_CANDIDATE,
            content_hash="sha256:output",
            payload=b"{}",
        )
    )

    first = ShadowArtifactModule().build(context, result)
    second = ShadowArtifactModule().build(context, result)

    assert first == second
    assert first is not None
    assert first.artifact_type == "SHADOW_EVALUATION"
    assert b'"extra_model_physical_calls":0' in first.content
    assert b'"assignment_hash":"sha256:assignment"' in first.content


def test_shadow_artifact_rejects_assignment_identity_drift() -> None:
    context = RunContext(
        run_id="run-shadow",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task",
        task_message="message",
        workflow_version="agent-runtime.v1",
        checkpoint=b"",
        evaluation_mode="SHADOW",
        authoritative_workflow_version="agent-runtime.v4",
        shadow_workflow_version="agent-runtime.v1",
        candidate_policy_version="policy-v4",
        assignment_hash="sha256:assignment",
    )

    with pytest.raises(ValueError, match="identity mismatch"):
        ShadowArtifactModule().build(context, AgentResult())
