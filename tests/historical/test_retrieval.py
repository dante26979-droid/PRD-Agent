from datetime import datetime, timezone

import pytest

from prd_agent.historical.models import (
    HistoricalPrdSearchArguments,
    RetrievalMode,
    RetrievalStatus,
)
from prd_agent.historical.retrieval import (
    HistoricalPrdRetriever,
    reciprocal_rank_fusion,
)

from .support import binding_for, write_fixture


def test_keyword_retrieval_filters_permissions_before_ranking_and_marks_stale(tmp_path):
    store, corpus = write_fixture(tmp_path)

    result = HistoricalPrdRetriever(store).search(
        investigation_id="investigation-1",
        binding=binding_for(corpus),
        arguments=HistoricalPrdSearchArguments(query="订单 completed"),
        owner_id="owner-1",
        project_id="project-1",
        as_of=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    assert result.run.status == RetrievalStatus.SUCCEEDED
    assert [item.document_id for item in result.items] == ["public-order"]
    assert result.items[0].stale_hint
    assert result.run.query_hash.startswith("sha256:")


def test_keyword_empty_does_not_claim_that_history_does_not_exist(tmp_path):
    store, corpus = write_fixture(tmp_path)

    result = HistoricalPrdRetriever(store).search(
        investigation_id="investigation-empty",
        binding=binding_for(corpus),
        arguments=HistoricalPrdSearchArguments(query="退款审批"),
        owner_id="owner-1",
        project_id="project-1",
    )

    assert result.run.status == RetrievalStatus.EMPTY
    assert result.items == ()
    assert "不存在" not in result.public_summary


def test_access_scope_change_invalidates_binding_instead_of_replaying_results(tmp_path):
    store, corpus = write_fixture(tmp_path)

    with pytest.raises(PermissionError, match="access scope"):
        HistoricalPrdRetriever(store).search(
            investigation_id="investigation-scope",
            binding=binding_for(corpus),
            arguments=HistoricalPrdSearchArguments(query="订单"),
            owner_id="owner-1",
            project_id="project-1",
            access_labels=("secret",),
        )


def test_hybrid_unavailable_is_explicit_and_fallback_is_traced(tmp_path):
    store, corpus = write_fixture(tmp_path)
    arguments = HistoricalPrdSearchArguments(
        query="订单", mode=RetrievalMode.HYBRID
    )
    retriever = HistoricalPrdRetriever(store)

    failed = retriever.search(
        investigation_id="investigation-hybrid-fail",
        binding=binding_for(corpus),
        arguments=arguments,
        owner_id="owner-1",
        project_id="project-1",
    )
    fallback = retriever.search(
        investigation_id="investigation-hybrid-fallback",
        binding=binding_for(corpus),
        arguments=arguments,
        owner_id="owner-1",
        project_id="project-1",
        allow_keyword_fallback=True,
    )

    assert failed.run.status == RetrievalStatus.FAILED
    assert failed.error_code == "HYBRID_UNAVAILABLE"
    assert fallback.run.mode == RetrievalMode.HYBRID
    assert fallback.run.fallback_mode == RetrievalMode.KEYWORD
    assert fallback.items


def test_rrf_uses_rank_positions_and_chunk_id_for_deterministic_ties():
    values = reciprocal_rank_fusion(("a", "b"), ("b", "c"))

    assert values[0][0] == "b"
    assert values[0][2:] == (2, 1)
    assert {item[0] for item in values} == {"a", "b", "c"}
