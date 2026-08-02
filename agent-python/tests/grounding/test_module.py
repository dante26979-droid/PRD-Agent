from __future__ import annotations

import hashlib
from dataclasses import replace

from agent.draft import Claim, ClaimCriticality, ClaimType, DraftBundle, DraftUnit
from agent.grounding import GroundingModule, GroundingStatus
from agent.investigation import CoverageStatus, ProposedAction
from agent.knowledge import (
    EvidenceKnowledgeModule,
    FactType,
    KnowledgeBuildRequest,
    SourceAuthority,
)
from agent.v1 import agent_execution_pb2 as proto


def test_historical_fact_cannot_support_current_state_claim() -> None:
    excerpt = "order route 已经存在"
    knowledge = EvidenceKnowledgeModule().build(
        KnowledgeBuildRequest(
            run_id="run-1",
            task_id="task-1",
            need_plan_id="need-1",
            need_context_hash="sha256:" + "1" * 64,
            action=ProposedAction(
                tool_id="search_prd_catalog",
                arguments={"query": "order route"},
                purpose="查找历史背景",
                target_coverage=("prd_evidence",),
            ),
            evidence_items=(
                proto.EvidenceItem(
                    source_type="prd",
                    source_id="revision-1",
                    locator="section-1",
                    excerpt_hash="sha256:"
                    + hashlib.sha256(excerpt.encode()).hexdigest(),
                    excerpt=excerpt,
                ),
            ),
            source_authorities=(
                SourceAuthority(
                    source_kind="prd",
                    binding_id="corpus-1",
                    source_id="revision-1",
                    source_version="revision-1",
                ),
            ),
            coverage={"prd_evidence": CoverageStatus.MISSING.value},
        )
    ).bundle
    claim = Claim(
        claim_id="claim-1",
        unit_key="current",
        claim_type=ClaimType.CURRENT_STATE,
        criticality=ClaimCriticality.BLOCKING,
        statement="order route 已经存在",
    )
    bundle = DraftBundle(
        schema_version="draft-bundle.v1",
        generation=1,
        units=(DraftUnit("current", "当前实现", "订单路由已经存在", 10),),
        claims=(claim,),
        unknowns=(),
        markdown="订单路由已经存在",
    )

    findings, _ = GroundingModule().assess(
        bundle,
        knowledge,
        supplement_count=1,
        max_supplements=1,
        has_remaining_tool_budget=True,
    )

    assert findings[0].status == GroundingStatus.UNSUPPORTED
    assert findings[0].reason_code == "FACT_SCOPE_MISMATCH"


def test_inferred_fact_cannot_be_upgraded_into_current_state_support() -> None:
    excerpt = "def order_route(): ..."
    knowledge = EvidenceKnowledgeModule().build(
        KnowledgeBuildRequest(
            run_id="run-1",
            task_id="task-1",
            need_plan_id="need-1",
            need_context_hash="sha256:" + "1" * 64,
            action=ProposedAction(
                tool_id="search_repository",
                arguments={"query": "order route"},
                purpose="定位订单路由",
                target_coverage=("repository_structure",),
            ),
            evidence_items=(
                proto.EvidenceItem(
                    source_type="github",
                    source_id="binding-1",
                    locator="github://binding-1@" + "a" * 40 + "/orders.py#L1",
                    excerpt_hash="sha256:"
                    + hashlib.sha256(excerpt.encode()).hexdigest(),
                    excerpt=excerpt,
                ),
            ),
            source_authorities=(
                SourceAuthority("github", "binding-1", "binding-1", "a" * 40),
            ),
            coverage={"repository_structure": CoverageStatus.MISSING.value},
        )
    ).bundle
    inferred = replace(
        knowledge,
        facts=(replace(knowledge.facts[0], fact_type=FactType.INFERRED),),
    )
    claim = Claim(
        claim_id="claim-1",
        unit_key="current",
        claim_type=ClaimType.CURRENT_STATE,
        criticality=ClaimCriticality.BLOCKING,
        statement="order route",
    )
    bundle = DraftBundle(
        schema_version="draft-bundle.v1",
        generation=1,
        units=(DraftUnit("current", "当前实现", "订单路由", 10),),
        claims=(claim,),
        unknowns=(),
        markdown="订单路由",
    )

    findings, _ = GroundingModule().assess(
        bundle,
        inferred,
        supplement_count=1,
        max_supplements=1,
        has_remaining_tool_budget=True,
    )

    assert findings[0].status == GroundingStatus.UNSUPPORTED
    assert findings[0].reason_code == "FACT_TYPE_MISMATCH"
