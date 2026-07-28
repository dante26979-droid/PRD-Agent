import unittest

from prd_agent.domain.entities import ConfirmationUnit, PrdSectionVersion
from prd_agent.domain.enums import SectionStatus
from prd_agent.workflow.context_builder import build_confirmed_context


class ContextBuilderTests(unittest.TestCase):
    def test_only_current_confirmed_direct_dependencies_enter_unit_context(self) -> None:
        target = ConfirmationUnit(
            "unit-3",
            "outline-1",
            3,
            "验收",
            depends_on_unit_ids=("unit-1",),
        )
        sections = (
            PrdSectionVersion(
                "section-old",
                "unit-1",
                1,
                "规则",
                "旧规则",
                node_id="node-1",
                status=SectionStatus.SUPERSEDED,
                content_hash="sha256:old",
            ),
            PrdSectionVersion(
                "section-current",
                "unit-1",
                2,
                "规则",
                "当前规则",
                node_id="node-1",
                status=SectionStatus.CONFIRMED,
                content_hash="sha256:current",
            ),
            PrdSectionVersion(
                "section-unrelated",
                "unit-2",
                1,
                "数据",
                "无关内容",
                node_id="node-2",
                status=SectionStatus.CONFIRMED,
                content_hash="sha256:unrelated",
            ),
        )

        context = build_confirmed_context(target, sections)

        self.assertEqual(len(context), 1)
        self.assertEqual(context[0]["section_id"], "section-current")
        self.assertEqual(context[0]["content"], "当前规则")
        self.assertNotIn("旧规则", str(context))
        self.assertNotIn("无关内容", str(context))


if __name__ == "__main__":
    unittest.main()
