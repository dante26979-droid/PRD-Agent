import unittest

from prd_agent.evidence.models import (
    DeterministicFact,
    ExtractionMethod,
    FactType,
    SourceEvidence,
    VerificationStatus,
)
from prd_agent.grounding.models import (
    ClaimCriticality,
    ClaimKind,
    GroundingRequest,
    PrdClaim,
)
from prd_agent.grounding.service import GroundingService
from prd_agent.rendering.evidence_appendix import render_evidence_appendix


class EvidenceAppendixTests(unittest.TestCase):
    def test_supported_reference_is_rendered_without_copying_source_excerpt(self) -> None:
        commit = "a" * 40
        evidence = SourceEvidence(
            evidence_id="evidence-1",
            tool_call_id="call-1",
            repository_id="demo",
            resolved_commit_sha=commit,
            path="src/orders/filter.py",
            line_start=10,
            line_end=14,
            excerpt="订单列表支持开始时间筛选。SECRET_SHOULD_NOT_APPEAR",
            content_hash="sha256:evidence",
            extraction_method=ExtractionMethod.SOURCE_READ,
        )
        fact = DeterministicFact(
            fact_id="fact-1",
            tool_call_id="call-1",
            task_id="task-1",
            subject="订单列表",
            predicate="支持",
            value_json="开始时间筛选",
            fact_type=FactType.CODE_VERIFIED,
            confidence="HIGH",
            verification_status=VerificationStatus.SUPPORTED,
            extractor_id="test",
            extractor_version="1",
            evidence_ids=("evidence-1",),
        )
        claim = PrdClaim(
            claim_id="claim-1",
            text="当前订单列表支持开始时间筛选。",
            kind=ClaimKind.CURRENT_STATE,
            criticality=ClaimCriticality.CRITICAL,
            fact_ids=("fact-1",),
        )
        result = GroundingService().ground(
            GroundingRequest(
                grounding_run_id="grounding-1",
                task_id="task-1",
                unit_id="unit-1",
                repository_id="demo",
                resolved_commit_sha=commit,
                content=claim.text,
                claims=(claim,),
                facts=(fact,),
                evidence=(evidence,),
            )
        )

        markdown = render_evidence_appendix((result,))

        self.assertIn("## 参考依据", markdown)
        self.assertIn("src/orders/filter.py:10-14", markdown)
        self.assertIn("aaaaaaaaaaaa", markdown)
        self.assertIn("claim-1", markdown)
        self.assertNotIn("SECRET_SHOULD_NOT_APPEAR", markdown)


if __name__ == "__main__":
    unittest.main()
