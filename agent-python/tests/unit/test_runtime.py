from __future__ import annotations

import json

import pytest

from agent.bootstrap import DeterministicStructuredModel
from agent.checkpoint import CheckpointCodec
from agent.context import RunContext
from agent.context_pack import ContextPolicy
from agent.graph import LangGraphAgentLoop
from agent.model import ModelResponse
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


def test_reviewable_runtime_normalizes_provider_outline_envelope() -> None:
    scope = UnitScope(purpose=RunPurpose.PLAN_OUTLINE)
    scope_proto = proto.UnitScope(
        schema_version=scope.schema_version,
        requirement_brief_ref=scope.requirement_brief_ref,
        requirement_brief_hash=scope.requirement_brief_hash,
        scope_hash=scope.scope_hash,
    )
    context = RunContext(
        run_id="run-outline",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task",
        task_message="最小单页部署验证",
        workflow_version="agent-runtime.v4",
        checkpoint=b"",
        task_version=1,
        run_purpose=proto.RUN_PURPOSE_PLAN_OUTLINE,
        unit_scope=scope_proto,
    )

    class ProviderEnvelopeModel:
        request: dict[str, object] | None = None

        def complete(self, system_prompt: str, user_prompt: str):
            self.request = json.loads(user_prompt)
            return ModelResponse(
                output=json.dumps(
                    {
                        "schema_version": "outline-candidate.v1",
                        "outline": {
                            "units": [
                                {
                                    "key": "unit-1",
                                    "title": "部署验证",
                                    "ordinal": 0,
                                    "dependencies": [],
                                    "content": {
                                        "goal": "验证部署",
                                        "acceptance_criteria": ["HTTP 200"],
                                    },
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                ),
                token_usage={"total_tokens": 1},
                model_id="provider-model",
                latency_ms=1,
            )

    model = ProviderEnvelopeModel()
    result = LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
    )(context)

    assert result.run_output is not None
    payload = json.loads(result.run_output.payload)
    assert payload["schema_version"] == "outline-candidate.v1"
    assert payload["title"] == "最小单页部署验证"
    assert payload["requirement_size"] == "SMALL"
    assert payload["nodes"][0]["node_key"] == "unit-1"
    assert payload["units"][0]["node_keys"] == ["unit-1"]
    assert model.request is not None
    contract = model.request["output_contract"]
    assert isinstance(contract, dict)
    assert "nodes" in contract


def test_reviewable_runtime_locks_provider_unit_identity_to_scope() -> None:
    class IdentityDriftingModel:
        def complete(self, system_prompt: str, user_prompt: str) -> ModelResponse:
            return ModelResponse(
                output=json.dumps(
                    {
                        "schema_version": "unit-candidate.v1",
                        "unit_key": "provider-invented-unit",
                        "title": "Provider invented title",
                        "ordinal": 0,
                        "node_keys": ["provider-invented-node"],
                        "markdown": "# 需求与验收\n\n## 验收标准\n\n- 输出必须遵守冻结范围。",
                        "claims": [],
                        "claim_ids": [],
                        "unknown_ids": [],
                        "used_fact_ids": [],
                    },
                    ensure_ascii=False,
                ),
                token_usage={"total_tokens": 1},
                model_id="provider-model",
            )

    result = LangGraphAgentLoop(
        model=IdentityDriftingModel(),
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
    )(_context(_scope_proto()))

    assert result.run_output is not None
    payload = json.loads(result.run_output.payload)
    assert payload["unit_key"] == "requirements"
    assert payload["title"] == "需求与验收"
    assert payload["ordinal"] == 1
    assert payload["node_keys"] == ["requirements"]


def test_reviewable_runtime_normalizes_provider_unit_output_contract_envelope() -> None:
    class OutputContractEnvelopeModel:
        def complete(self, system_prompt: str, user_prompt: str) -> ModelResponse:
            return ModelResponse(
                output=json.dumps(
                    {
                        "base_candidate": None,
                        "expected_schema": "unit-candidate.v1",
                        "instruction": (
                            "Return only the requested schema and do not write "
                            "outside current_unit_key."
                        ),
                        "output_contract": {
                            "schema_version": "unit-candidate.v1",
                            "unit_key": "requirements",
                            "title": "需求与验收",
                            "ordinal": 1,
                            "node_keys": ["requirements"],
                            "markdown": "## 目标\n\n完成生产冒烟验证。\n\n## 验收标准\n\n- 完整流程通过。",
                            "claims": [],
                            "claim_ids": [],
                            "unknown_ids": [],
                            "used_fact_ids": [],
                        },
                    },
                    ensure_ascii=False,
                ),
                token_usage={"total_tokens": 1},
                model_id="provider-model",
            )

    result = LangGraphAgentLoop(
        model=OutputContractEnvelopeModel(),
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
    )(_context(_scope_proto()))

    assert result.run_output is not None
    payload = json.loads(result.run_output.payload)
    assert payload["unit_key"] == "requirements"
    assert payload["markdown"].startswith("# 需求与验收\n\n## 目标")


def test_reviewable_runtime_normalizes_provider_claim_content() -> None:
    class ProviderClaimModel:
        def complete(self, system_prompt: str, user_prompt: str) -> ModelResponse:
            return ModelResponse(
                output=json.dumps(
                    {
                        "schema_version": "unit-candidate.v1",
                        "unit_key": "provider-unit",
                        "title": "Provider title",
                        "ordinal": 0,
                        "node_keys": ["provider-node"],
                        "markdown": "# Provider title\n\n## 验收标准\n\n- 流程通过。",
                        "claims": [
                            {
                                "claim_id": "claim-0",
                                "content": "流程必须通过。",
                                "verification_criteria": "任务进入 REVIEWABLE。",
                            }
                        ],
                        "claim_ids": ["claim-0"],
                        "unknown_ids": [],
                        "used_fact_ids": [],
                    },
                    ensure_ascii=False,
                ),
                token_usage={"total_tokens": 1},
                model_id="provider-model",
            )

    result = LangGraphAgentLoop(
        model=ProviderClaimModel(),
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
    )(_context(_scope_proto()))

    assert result.run_output is not None
    payload = json.loads(result.run_output.payload)
    assert payload["claims"] == [
        {
            "unit_key": "requirements",
            "claim_type": "PROPOSED_BEHAVIOR",
            "criticality": "IMPORTANT",
            "statement": "流程必须通过。",
            "evidence_refs": [],
        }
    ]


def test_scoped_v4_path_applies_context_policy_even_without_entering_state_graph() -> None:
    sink = BufferedRuntimeEventSink()
    base = _context(_scope_proto())
    context = RunContext(
        **{
            **base.__dict__,
            "event_sink": sink,
            "execution_ledger_version": "run-ledger.v1",
            "run_budget": proto.RunBudget(
                max_model_attempts=4,
                max_tool_calls=2,
                max_iterations=2,
                max_replans=1,
                max_supplements=1,
                max_quality_repairs=1,
                max_input_tokens=50_000,
                max_output_tokens=10_000,
                max_elapsed_ms=60_000,
            ),
            "consumed_budget": proto.ConsumedBudget(),
        }
    )

    class CapturingModel(DeterministicStructuredModel):
        request: dict[str, object] | None = None

        def complete(self, system_prompt: str, user_prompt: str) -> ModelResponse:
            self.request = json.loads(user_prompt)
            return super().complete(system_prompt, user_prompt)

    model = CapturingModel()
    result = LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        context_policy=ContextPolicy(
            version="context-policy.v1",
            mode="enforce",
            context_window_tokens=8_000,
            compact_threshold_tokens=5_000,
            target_input_tokens=4_000,
            reserved_output_tokens=1_000,
            emergency_margin_tokens=500,
        ),
    )(context)

    assert result.run_output is not None
    assert model.request is not None
    assert model.request["context_pack"]["policy_version"] == "context-policy.v1"
    assert [item.artifact_type for item in sink.artifacts[:2]] == [
        "CONTEXT_PACK",
        "MODEL_VALIDATED_OUTPUT",
    ]
