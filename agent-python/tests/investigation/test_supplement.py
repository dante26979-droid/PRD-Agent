from __future__ import annotations

import pytest

from agent.information_need import NeedBudgetAllocation, SourceType
from agent.investigation import SupplementNeedFactory


def test_supplement_need_is_a_stable_subset_of_the_parent_need() -> None:
    factory = SupplementNeedFactory()
    first = factory.build(
        parent_plan_id="need-parent",
        grounding_report_fingerprint="sha256:" + "1" * 64,
        claim_ids=("claim-2", "claim-1"),
        question="订单路由是否已经存在？",
        requested_coverage=("repository_structure",),
        parent_coverage=("repository_structure", "tests"),
        requested_source_types=(SourceType.CODE,),
        parent_source_types=(SourceType.CODE,),
        remaining_budget=NeedBudgetAllocation(2, 1, 1, 0),
    )
    replay = factory.build(
        parent_plan_id="need-parent",
        grounding_report_fingerprint="sha256:" + "1" * 64,
        claim_ids=("claim-1", "claim-2"),
        question="订单路由是否已经存在？",
        requested_coverage=("repository_structure",),
        parent_coverage=("repository_structure", "tests"),
        requested_source_types=(SourceType.CODE,),
        parent_source_types=(SourceType.CODE,),
        remaining_budget=NeedBudgetAllocation(2, 1, 1, 0),
    )

    assert first == replay
    assert first.required_coverage == ("repository_structure",)
    assert first.claim_ids == ("claim-1", "claim-2")

    with pytest.raises(ValueError, match="expand parent coverage"):
        factory.build(
            parent_plan_id="need-parent",
            grounding_report_fingerprint="sha256:" + "1" * 64,
            claim_ids=("claim-1",),
            question="订单路由是否已经存在？",
            requested_coverage=("authorization_rule",),
            parent_coverage=("repository_structure", "tests"),
            requested_source_types=(SourceType.CODE,),
            parent_source_types=(SourceType.CODE,),
            remaining_budget=NeedBudgetAllocation(2, 1, 1, 0),
        )
