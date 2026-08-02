from __future__ import annotations

from agent.context import RunContext
from agent.draft import ClaimCriticality, ClaimType
from agent.runtime import BufferedRuntimeEventSink
from agent.unit.models import RunPurpose, UnitCandidate, UnitClaim, UnitScope
from agent.unit.semantic import ScopedUnitSemanticModule
from agent.v1 import agent_execution_pb2 as proto


def _context(scope: UnitScope, sink: BufferedRuntimeEventSink) -> RunContext:
    return RunContext(
        run_id="run-semantic",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task",
        task_message="生成 PRD",
        workflow_version="agent-runtime.v4",
        checkpoint=b"",
        task_version=4,
        run_purpose=proto.RUN_PURPOSE_GENERATE_UNIT,
        unit_scope=proto.UnitScope(
            schema_version=scope.schema_version,
            outline_id=scope.outline_id,
            outline_version=scope.outline_version,
            outline_hash=scope.outline_hash,
            current_unit_key=scope.current_unit_key,
            current_unit_title=scope.current_unit_title,
            current_unit_ordinal=scope.current_unit_ordinal,
            section_node_keys=scope.section_node_keys,
            scope_hash=scope.scope_hash,
        ),
        event_sink=sink,
    )


def test_current_state_claim_without_verified_fact_fails_closed_and_persists_artifacts() -> None:
    scope = UnitScope(
        purpose=RunPurpose.GENERATE_UNIT,
        outline_id="outline-1",
        outline_version=1,
        outline_hash="a" * 64,
        current_unit_key="current-state",
        current_unit_title="当前状态",
        current_unit_ordinal=10,
        section_node_keys=("current-state",),
    )
    sink = BufferedRuntimeEventSink()
    candidate = UnitCandidate(
        "current-state",
        "当前状态",
        10,
        ("current-state",),
        "# 当前状态\n\n系统当前使用旧审批流。",
        claims=(
            UnitClaim(
                ClaimType.CURRENT_STATE,
                ClaimCriticality.BLOCKING,
                "系统当前使用旧审批流",
            ),
        ),
    )

    findings = ScopedUnitSemanticModule(_context(scope, sink), sink).evaluate(candidate)

    assert findings[0]["status"] == "UNSUPPORTED"
    assert findings[0]["claim_type"] == "CURRENT_STATE"
    assert [item.artifact_type for item in sink.artifacts] == [
        "UNIT_CLAIM_SET",
        "UNIT_GROUNDING_REPORT",
    ]


def test_proposed_behavior_does_not_require_repository_grounding() -> None:
    scope = UnitScope(
        purpose=RunPurpose.GENERATE_UNIT,
        outline_id="outline-1",
        outline_version=1,
        outline_hash="b" * 64,
        current_unit_key="target",
        current_unit_title="目标方案",
        current_unit_ordinal=10,
        section_node_keys=("target",),
    )
    sink = BufferedRuntimeEventSink()
    candidate = UnitCandidate(
        "target",
        "目标方案",
        10,
        ("target",),
        "# 目标方案\n\n系统应支持逐单元评审。",
        claims=(
            UnitClaim(
                ClaimType.PROPOSED_BEHAVIOR,
                ClaimCriticality.IMPORTANT,
                "系统应支持逐单元评审",
            ),
        ),
    )

    findings = ScopedUnitSemanticModule(_context(scope, sink), sink).evaluate(candidate)

    assert findings[0]["status"] == "NOT_REQUIRED"
