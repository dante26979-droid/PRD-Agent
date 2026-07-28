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
    GroundingSupplement,
    GroundingVerdict,
    PrdClaim,
)
from prd_agent.grounding.service import GroundingService


class GroundingServiceTests(unittest.TestCase):
    def test_current_state_claim_is_confirmable_only_with_supporting_evidence(self) -> None:
        commit = "a" * 40
        evidence = SourceEvidence(
            evidence_id="evidence-1",
            tool_call_id="call-1",
            repository_id="demo",
            resolved_commit_sha=commit,
            path="src/orders/filter.py",
            line_start=10,
            line_end=14,
            excerpt="订单列表支持开始时间和结束时间筛选。",
            content_hash="sha256:evidence",
            extraction_method=ExtractionMethod.SOURCE_READ,
        )
        fact = DeterministicFact(
            fact_id="fact-1",
            tool_call_id="call-1",
            task_id="task-1",
            subject="订单列表",
            predicate="支持",
            value_json="开始时间和结束时间筛选",
            fact_type=FactType.CODE_VERIFIED,
            confidence="HIGH",
            verification_status=VerificationStatus.SUPPORTED,
            extractor_id="test",
            extractor_version="1",
            evidence_ids=("evidence-1",),
        )
        claim = PrdClaim(
            claim_id="claim-1",
            text="当前订单列表支持开始时间和结束时间筛选。",
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

        self.assertTrue(result.confirmable)
        self.assertEqual(result.fact_assessments[0].verdict, GroundingVerdict.SUPPORTED)
        self.assertEqual(result.claim_assessments[0].verdict, GroundingVerdict.SUPPORTED)
        self.assertEqual(result.used_fact_ids, ("fact-1",))

    def test_evidence_from_another_commit_cannot_support_a_claim(self) -> None:
        evidence = SourceEvidence(
            evidence_id="evidence-stale",
            tool_call_id="call-1",
            repository_id="demo",
            resolved_commit_sha="b" * 40,
            path="src/orders/filter.py",
            excerpt="订单列表支持开始时间筛选。",
            content_hash="sha256:evidence",
            extraction_method=ExtractionMethod.SOURCE_READ,
        )
        fact = DeterministicFact(
            fact_id="fact-stale",
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
            evidence_ids=("evidence-stale",),
        )
        claim = PrdClaim(
            claim_id="claim-stale",
            text="当前订单列表支持开始时间筛选。",
            kind=ClaimKind.CURRENT_STATE,
            criticality=ClaimCriticality.CRITICAL,
            fact_ids=("fact-stale",),
        )

        result = GroundingService().ground(
            GroundingRequest(
                grounding_run_id="grounding-stale",
                task_id="task-1",
                unit_id="unit-1",
                repository_id="demo",
                resolved_commit_sha="a" * 40,
                content=claim.text,
                claims=(claim,),
                facts=(fact,),
                evidence=(evidence,),
            )
        )

        self.assertFalse(result.confirmable)
        self.assertEqual(result.fact_assessments[0].verdict, GroundingVerdict.STALE_SOURCE)
        self.assertEqual(
            result.claim_assessments[0].verdict,
            GroundingVerdict.UNSUPPORTED,
        )

    def test_critical_claim_uses_at_most_one_targeted_supplement(self) -> None:
        commit = "a" * 40
        claim = PrdClaim(
            claim_id="claim-retry",
            text="当前订单列表支持结束时间筛选。",
            kind=ClaimKind.CURRENT_STATE,
            criticality=ClaimCriticality.CRITICAL,
            fact_ids=("fact-retry",),
        )
        evidence = SourceEvidence(
            evidence_id="evidence-retry",
            tool_call_id="call-retry",
            repository_id="demo",
            resolved_commit_sha=commit,
            path="src/orders/filter.py",
            excerpt="订单列表支持结束时间筛选。",
            content_hash="sha256:retry",
            extraction_method=ExtractionMethod.SOURCE_READ,
        )
        fact = DeterministicFact(
            fact_id="fact-retry",
            tool_call_id="call-retry",
            task_id="task-1",
            subject="订单列表",
            predicate="支持",
            value_json="结束时间筛选",
            fact_type=FactType.CODE_VERIFIED,
            confidence="HIGH",
            verification_status=VerificationStatus.SUPPORTED,
            extractor_id="test",
            extractor_version="1",
            evidence_ids=("evidence-retry",),
        )

        class SupplementProvider:
            def __init__(self) -> None:
                self.calls = 0

            def supplement(self, request, failed_claim_ids):
                self.calls += 1
                self.assert_request = request
                self.failed_claim_ids = failed_claim_ids
                return GroundingSupplement(facts=(fact,), evidence=(evidence,))

        provider = SupplementProvider()
        result = GroundingService(retry_provider=provider).ground(
            GroundingRequest(
                grounding_run_id="grounding-retry",
                task_id="task-1",
                unit_id="unit-1",
                repository_id="demo",
                resolved_commit_sha=commit,
                content=claim.text,
                claims=(claim,),
            )
        )

        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.failed_claim_ids, ("claim-retry",))
        self.assertTrue(result.confirmable)
        self.assertEqual(result.retry_count, 1)

    def test_failed_supplement_does_not_start_a_second_retry(self) -> None:
        claim = PrdClaim(
            claim_id="claim-missing",
            text="当前系统支持批量导入。",
            kind=ClaimKind.CURRENT_STATE,
            criticality=ClaimCriticality.CRITICAL,
            fact_ids=("fact-missing",),
        )

        class EmptySupplementProvider:
            def __init__(self) -> None:
                self.calls = 0

            def supplement(self, request, failed_claim_ids):
                self.calls += 1
                return GroundingSupplement()

        provider = EmptySupplementProvider()
        result = GroundingService(retry_provider=provider).ground(
            GroundingRequest(
                grounding_run_id="grounding-missing",
                task_id="task-1",
                unit_id="unit-1",
                repository_id="demo",
                resolved_commit_sha="a" * 40,
                content=claim.text,
                claims=(claim,),
            )
        )

        self.assertEqual(provider.calls, 1)
        self.assertFalse(result.confirmable)
        self.assertEqual(result.retry_count, 1)

    def test_material_unsupported_current_state_claim_is_not_silently_degraded(
        self,
    ) -> None:
        claim = PrdClaim(
            claim_id="claim-material",
            text="当前系统支持批量导入。",
            kind=ClaimKind.CURRENT_STATE,
            criticality=ClaimCriticality.MATERIAL,
            fact_ids=("fact-missing",),
        )
        result = GroundingService().ground(
            GroundingRequest(
                grounding_run_id="grounding-material",
                task_id="task-1",
                unit_id="unit-1",
                repository_id="demo",
                resolved_commit_sha="a" * 40,
                content=claim.text,
                claims=(claim,),
            )
        )

        self.assertFalse(result.confirmable)


if __name__ == "__main__":
    unittest.main()
