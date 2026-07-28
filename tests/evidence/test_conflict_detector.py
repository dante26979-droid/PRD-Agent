import unittest

from prd_agent.evidence.conflict_detector import DeterministicConflictDetector
from prd_agent.evidence.models import (
    DeterministicFact,
    FactType,
    VerificationStatus,
)


def fact(fact_id: str, value: str) -> DeterministicFact:
    return DeterministicFact(
        fact_id=fact_id,
        tool_call_id="call",
        subject="orders.status",
        predicate="database_column_contract",
        value_json={"default": value},
        fact_type=FactType.CODE_VERIFIED,
        confidence="HIGH",
        verification_status=VerificationStatus.SUPPORTED,
        extractor_id="parse_database_schema",
        extractor_version="1",
        evidence_ids=("evidence",),
    )


class ConflictDetectorTests(unittest.TestCase):
    def test_preserves_both_parser_facts_and_marks_exact_value_conflict(self) -> None:
        facts, conflicts = DeterministicConflictDetector().detect(
            (fact("fact-a", "pending"), fact("fact-b", "created"))
        )

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(set(conflicts[0].fact_ids), {"fact-a", "fact-b"})
        self.assertTrue(
            all(item.verification_status == VerificationStatus.CONFLICTING for item in facts)
        )


if __name__ == "__main__":
    unittest.main()
