from __future__ import annotations

import pytest

from agent.investigation import InvestigationPlan
from agent.quality import DraftQualityError, DraftQualityPolicy


def test_investigation_plan_is_bounded_and_deduplicated():
    plan = InvestigationPlan.from_model_output(
        {
            "repository_queries": ["dispatcher", "dispatcher", "lease"],
            "prd_query": "Agent 边界",
        },
        max_repository_queries=2,
    )

    assert plan.repository_queries == ("dispatcher", "lease")
    assert plan.prd_query == "Agent 边界"


def test_draft_quality_policy_rejects_secret_material():
    with pytest.raises(DraftQualityError, match="sensitive marker"):
        DraftQualityPolicy().validate(
            "# PRD\n\nAuthorization: Bearer secret-value",
        )
