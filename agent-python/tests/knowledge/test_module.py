from __future__ import annotations

import hashlib
import json

from agent.investigation import CoverageStatus, ProposedAction
from agent.knowledge import (
    EvidenceKnowledgeModule,
    KnowledgeArtifactCodec,
    KnowledgeBuildRequest,
    SourceAuthority,
)
from agent.v1 import agent_execution_pb2 as proto


def test_related_evidence_does_not_complete_coverage() -> None:
    excerpt = "def calculate_order_total(): ..."
    action = ProposedAction(
        tool_id="search_repository",
        arguments={"query": "order route"},
        purpose="定位订单路由",
        target_coverage=("repository_structure",),
    )
    result = EvidenceKnowledgeModule().build(
        KnowledgeBuildRequest(
            run_id="run-1",
            task_id="task-1",
            need_plan_id="need-1",
            need_context_hash="sha256:" + "1" * 64,
            action=action,
            evidence_items=(
                proto.EvidenceItem(
                    source_type="github",
                    source_id="binding-1",
                    locator="github://binding-1@" + "a" * 40 + "/api/orders.py#L10",
                    excerpt_hash="sha256:"
                    + hashlib.sha256(excerpt.encode()).hexdigest(),
                    excerpt=excerpt,
                ),
            ),
            source_authorities=(
                SourceAuthority(
                    source_kind="github",
                    binding_id="binding-1",
                    source_id="binding-1",
                    source_version="a" * 40,
                ),
            ),
            coverage={"repository_structure": CoverageStatus.MISSING.value},
        )
    )

    assert result.bundle.facts == ()
    assert result.bundle.unknowns[0].reason_code == "UNSUPPORTED_EVIDENCE"
    assert result.coverage == {
        "repository_structure": CoverageStatus.MISSING.value
    }
    assert result.progressed is True


def test_conflicting_deterministic_facts_block_coverage() -> None:
    excerpts = (
        json.dumps(
            {
                "subject": "POST /orders.request.currency",
                "predicate": "required",
                "value": True,
                "scope": "CURRENT_STATE",
            },
            sort_keys=True,
        ),
        json.dumps(
            {
                "subject": "POST /orders.request.currency",
                "predicate": "required",
                "value": False,
                "scope": "CURRENT_STATE",
            },
            sort_keys=True,
        ),
    )
    items = tuple(
        proto.EvidenceItem(
            source_type="openapi",
            source_id="binding-1",
            locator=f"openapi://binding-1@{'a' * 40}/orders.yaml#{index}",
            excerpt_hash="sha256:" + hashlib.sha256(excerpt.encode()).hexdigest(),
            excerpt=excerpt,
        )
        for index, excerpt in enumerate(excerpts, 1)
    )
    result = EvidenceKnowledgeModule().build(
        KnowledgeBuildRequest(
            run_id="run-1",
            task_id="task-1",
            need_plan_id="need-1",
            need_context_hash="sha256:" + "1" * 64,
            action=ProposedAction(
                tool_id="parse_openapi",
                arguments={"query": "currency required"},
                purpose="确认 currency 是否必填",
                target_coverage=("api_contract",),
            ),
            evidence_items=items,
            source_authorities=(
                SourceAuthority(
                    source_kind="openapi",
                    binding_id="binding-1",
                    source_id="binding-1",
                    source_version="a" * 40,
                ),
            ),
            coverage={"api_contract": CoverageStatus.MISSING.value},
        )
    )

    assert len(result.bundle.conflicts) == 1
    assert {fact.verification_status for fact in result.bundle.facts} == {
        "CONFLICTING"
    }
    assert result.coverage["api_contract"] == CoverageStatus.CONFLICTING.value


def test_knowledge_artifact_round_trip_preserves_identity() -> None:
    excerpt = "def order_route(): ..."
    result = EvidenceKnowledgeModule().build(
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
    )

    artifact = KnowledgeArtifactCodec().encode(result.bundle, generation=1)
    restored = KnowledgeArtifactCodec().decode(artifact)

    assert restored == result.bundle
    assert artifact.artifact_type == "KNOWLEDGE_BUNDLE"
    assert artifact.request_hash == result.bundle.knowledge_fingerprint
