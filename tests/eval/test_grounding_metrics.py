import unittest

from prd_agent.eval.metrics import evaluate_grounding
from prd_agent.grounding.models import (
    ClaimCriticality,
    ClaimKind,
    GroundingRequest,
    PrdClaim,
)
from prd_agent.grounding.service import GroundingService


class GroundingMetricTests(unittest.TestCase):
    def test_unsupported_claim_rate_is_measured_for_grounding_results(self) -> None:
        supported_label = PrdClaim(
            claim_id="claim-risk",
            text="待确认批量导入能力。",
            kind=ClaimKind.RISK,
            criticality=ClaimCriticality.MATERIAL,
        )
        unsupported_current = PrdClaim(
            claim_id="claim-current",
            text="当前支持批量导入。",
            kind=ClaimKind.CURRENT_STATE,
            criticality=ClaimCriticality.CRITICAL,
            fact_ids=("fact-missing",),
        )
        result = GroundingService().ground(
            GroundingRequest(
                grounding_run_id="grounding-metrics",
                task_id="task-1",
                unit_id="unit-1",
                repository_id="demo",
                resolved_commit_sha="a" * 40,
                content="待确认批量导入能力。当前支持批量导入。",
                claims=(supported_label, unsupported_current),
            )
        )

        metrics = {item.name: item for item in evaluate_grounding(result)}

        self.assertEqual(metrics["unsupported_claim_rate"].status, "measured")
        self.assertEqual(metrics["unsupported_claim_rate"].value, 1.0)
        self.assertEqual(metrics["grounding_retry_rate"].value, 0.0)


if __name__ == "__main__":
    unittest.main()
