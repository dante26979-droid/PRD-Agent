from prd_agent.evidence.models import FactScope, FactType
from prd_agent.grounding.models import (
    ClaimKind,
    GroundingRequest,
    GroundingVerdict,
    PrdClaim,
)
from prd_agent.grounding.service import GroundingService
from prd_agent.historical.evidence import evidence_from_result, historical_fact
from prd_agent.historical.models import HistoricalPrdSearchArguments
from prd_agent.historical.retrieval import HistoricalPrdRetriever

from .support import binding_for, write_fixture


def _historical_grounding_fixture(tmp_path):
    store, corpus = write_fixture(tmp_path)
    binding = binding_for(corpus)
    result = HistoricalPrdRetriever(store).search(
        investigation_id="investigation-ground",
        binding=binding,
        arguments=HistoricalPrdSearchArguments(query="completed"),
        owner_id="owner-1",
        project_id="project-1",
    )
    evidence = evidence_from_result(
        result.items[0],
        tool_call_id="historical-call-1",
        binding=binding,
    )
    fact = historical_fact(
        evidence=evidence,
        task_id="task-1",
        subject="订单完成态",
        predicate="历史命名",
        value="completed",
    )
    return binding, evidence, fact


def test_historical_document_fact_supports_only_historical_claim(tmp_path):
    binding, evidence, fact = _historical_grounding_fixture(tmp_path)
    claim = PrdClaim(
        claim_id="claim-history",
        text="历史方案使用 completed。",
        kind=ClaimKind.HISTORICAL_CONTEXT,
        fact_ids=(fact.fact_id,),
    )

    result = GroundingService().ground(
        GroundingRequest(
            grounding_run_id="grounding-history",
            task_id="task-1",
            unit_id="unit-1",
            allowed_source_bindings=(binding,),
            content=claim.text,
            claims=(claim,),
            facts=(fact,),
            evidence=(evidence,),
        )
    )

    assert fact.fact_type == FactType.DOCUMENT_SUPPORTED
    assert fact.fact_scope == FactScope.HISTORICAL_CONTEXT
    assert result.confirmable
    assert result.claim_assessments[0].verdict == GroundingVerdict.SUPPORTED
    assert result.references[0].source_id == "public-order"


def test_historical_document_fact_cannot_promote_a_current_state_claim(tmp_path):
    binding, evidence, fact = _historical_grounding_fixture(tmp_path)
    claim = PrdClaim(
        claim_id="claim-current",
        text="当前订单完成态是 completed。",
        kind=ClaimKind.CURRENT_STATE,
        fact_ids=(fact.fact_id,),
    )

    result = GroundingService().ground(
        GroundingRequest(
            grounding_run_id="grounding-current",
            task_id="task-1",
            unit_id="unit-1",
            allowed_source_bindings=(binding,),
            content=claim.text,
            claims=(claim,),
            facts=(fact,),
            evidence=(evidence,),
        )
    )

    assert not result.confirmable
    assert result.claim_assessments[0].verdict == GroundingVerdict.UNSUPPORTED


def test_historical_failure_does_not_call_repository_only_retry_provider(tmp_path):
    binding, _, _ = _historical_grounding_fixture(tmp_path)

    class RetryProvider:
        def __init__(self):
            self.calls = 0

        def supplement(self, request, failed_claim_ids):
            self.calls += 1

    provider = RetryProvider()
    claim = PrdClaim(
        claim_id="claim-history-missing",
        text="历史方案包含退款规则。",
        kind=ClaimKind.HISTORICAL_CONTEXT,
        fact_ids=("missing-fact",),
    )
    result = GroundingService(retry_provider=provider).ground(
        GroundingRequest(
            grounding_run_id="grounding-history-missing",
            task_id="task-1",
            unit_id="unit-1",
            allowed_source_bindings=(binding,),
            content=claim.text,
            claims=(claim,),
        )
    )

    assert not result.confirmable
    assert provider.calls == 0
