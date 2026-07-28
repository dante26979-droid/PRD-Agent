import unittest

from prd_agent.evidence.fact_builder import DeterministicFactBuilder
from prd_agent.evidence.models import FactType, VerificationStatus
from prd_agent.evidence.normalizer import EvidenceNormalizer
from prd_agent.tools.models import ToolAction, ToolResult, ToolResultItem, ToolStatus


class DeterministicFactBuilderTests(unittest.TestCase):
    def test_only_parser_item_can_become_supported_code_fact(self) -> None:
        action = ToolAction(
            tool_id="parse_database_schema",
            tool_schema_version="1",
            repository_id="demo",
            resolved_commit_sha="a" * 40,
            arguments={"path": "db/schema.sql", "table": "orders"},
            purpose="确认存储字段",
        )
        result = ToolResult(
            status=ToolStatus.SUCCEEDED,
            items=(
                ToolResultItem(
                    kind="database_column",
                    path="db/schema.sql",
                    line_start=2,
                    line_end=2,
                    excerpt="amount NUMERIC(12, 2) NOT NULL",
                    blob_id="b" * 40,
                    metadata={
                        "table": "orders",
                        "column": "amount",
                        "type": "DECIMAL(12, 2)",
                        "nullable": False,
                    },
                ),
            ),
            public_summary="字段",
        )
        bundle = EvidenceNormalizer().normalize("call-1", action, result)

        facts = DeterministicFactBuilder().build(action, result, bundle.evidence)

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].fact_type, FactType.CODE_VERIFIED)
        self.assertEqual(facts[0].verification_status, VerificationStatus.SUPPORTED)
        self.assertEqual(facts[0].subject, "orders.amount")
        self.assertEqual(facts[0].evidence_ids, (bundle.evidence[0].evidence_id,))


if __name__ == "__main__":
    unittest.main()
