import unittest

from prd_agent.domain.entities import (
    ConfirmationUnit,
    OutlineNode,
    OutlineVersion,
    PrdSectionVersion,
)
from prd_agent.domain.enums import (
    Complexity,
    OutlineStatus,
    SectionStatus,
    UnitStatus,
)
from prd_agent.quality.models import QualityIssueType, QualityScope, QualitySeverity
from prd_agent.quality.service import DocumentQualityService


class DocumentQualityServiceTests(unittest.TestCase):
    def test_missing_confirmed_outline_node_blocks_document_confirmation(self) -> None:
        first_node = OutlineNode(
            "node-1",
            1,
            "规则",
            "定义规则",
            Complexity.MEDIUM,
        )
        second_node = OutlineNode(
            "node-2",
            2,
            "验收",
            "定义验收",
            Complexity.MEDIUM,
        )
        outline = OutlineVersion(
            outline_id="outline-1",
            task_id="task-1",
            version=1,
            title="订单筛选",
            status=OutlineStatus.CONFIRMED,
            nodes=(first_node, second_node),
            confirmation_units=(
                ConfirmationUnit(
                    "unit-1",
                    "outline-1",
                    1,
                    "规则",
                    UnitStatus.CONFIRMED,
                    node_ids=("node-1",),
                ),
                ConfirmationUnit(
                    "unit-2",
                    "outline-1",
                    2,
                    "验收",
                    UnitStatus.CONFIRMED,
                    node_ids=("node-2",),
                ),
            ),
        )
        sections = (
            PrdSectionVersion(
                "section-1",
                "unit-1",
                1,
                "规则",
                "- 支持时间筛选",
                node_id="node-1",
                status=SectionStatus.CONFIRMED,
            ),
        )

        result = DocumentQualityService().check_document(
            quality_run_id="quality-1",
            task_id="task-1",
            document_id="document-1",
            outline=outline,
            sections=sections,
        )

        self.assertFalse(result.confirmable)
        self.assertEqual(result.scope, QualityScope.DOCUMENT)
        self.assertEqual(result.issues[0].issue_type, QualityIssueType.OUTLINE_COVERAGE)
        self.assertEqual(result.issues[0].severity, QualitySeverity.BLOCKER)
        self.assertEqual(result.issues[0].affected_node_ids, ("node-2",))


if __name__ == "__main__":
    unittest.main()
