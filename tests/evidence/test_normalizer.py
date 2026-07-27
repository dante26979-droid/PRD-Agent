import hashlib
import unittest

from prd_agent.evidence.normalizer import EvidenceNormalizer
from prd_agent.evidence.models import UnknownReason
from prd_agent.tools.models import ToolAction, ToolResult, ToolResultItem, ToolStatus


class EvidenceNormalizerTests(unittest.TestCase):
    def test_builds_minimal_evidence_with_exact_hash_without_promoting_text(self) -> None:
        action = ToolAction(
            tool_id="search_text",
            tool_schema_version="1",
            repository_id="demo",
            resolved_commit_sha="a" * 40,
            arguments={"query": "amount"},
            purpose="确认金额规则",
        )
        result = ToolResult(
            status=ToolStatus.SUCCEEDED,
            items=(
                ToolResultItem(
                    kind="text_match",
                    path="src/validators.py",
                    line_start=2,
                    line_end=2,
                    excerpt="if amount <= 0:",
                    blob_id="b" * 40,
                ),
            ),
            public_summary="一个匹配",
        )

        bundle = EvidenceNormalizer().normalize("call-1", action, result)

        self.assertEqual(len(bundle.evidence), 1)
        evidence = bundle.evidence[0]
        expected = "sha256:" + hashlib.sha256(b"if amount <= 0:").hexdigest()
        self.assertEqual(evidence.content_hash, expected)
        self.assertEqual(evidence.resolved_commit_sha, "a" * 40)
        self.assertEqual(bundle.facts, ())

    def test_marks_parser_partial_as_parse_unsupported_unknown(self) -> None:
        action = ToolAction(
            tool_id="parse_database_schema",
            tool_schema_version="1",
            repository_id="demo",
            resolved_commit_sha="a" * 40,
            arguments={"path": "db/schema.sql"},
            purpose="确认 Schema 解析完整性",
        )
        result = ToolResult(
            status=ToolStatus.PARTIAL,
            items=(
                ToolResultItem(
                    kind="database_column",
                    path="db/schema.sql",
                    line_start=1,
                    line_end=1,
                    excerpt="id BIGINT",
                    metadata={"table": "orders", "column": "id"},
                ),
            ),
            public_summary="部分 DDL 已解析",
            error_code="PARSE_UNSUPPORTED",
        )

        bundle = EvidenceNormalizer().normalize("call-parser", action, result)

        self.assertEqual(len(bundle.unknowns), 1)
        self.assertEqual(bundle.unknowns[0].reason, UnknownReason.PARSE_UNSUPPORTED)


if __name__ == "__main__":
    unittest.main()
