import unittest

from prd_agent.domain.commands import StartTask
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService

from .support import ScriptedModel, sufficient_brief


class OutlinePlanTests(unittest.TestCase):
    def test_three_level_outline_preserves_parent_relationships(self) -> None:
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [
                    {
                        "title": "订单筛选",
                        "nodes": [
                            {
                                "key": "flow",
                                "title": "流程",
                                "purpose": "定义流程",
                                "complexity": "HIGH",
                            },
                            {
                                "key": "normal",
                                "parent_key": "flow",
                                "title": "正常流程",
                                "purpose": "定义正常路径",
                                "complexity": "MEDIUM",
                            },
                            {
                                "key": "validation",
                                "parent_key": "normal",
                                "title": "参数校验",
                                "purpose": "定义校验",
                                "complexity": "MEDIUM",
                            },
                        ],
                        "units": [
                            {
                                "key": "unit-flow",
                                "title": "流程",
                                "node_keys": ["flow", "normal", "validation"],
                                "depends_on_unit_keys": [],
                            }
                        ],
                    }
                ],
            }
        )
        service = WorkflowService(InMemoryWorkflowRepository(), model)

        waiting = service.start_task(StartTask("增加时间筛选", "nested-start"))

        nodes = waiting.current_outline.nodes
        self.assertEqual([item.level for item in nodes], [1, 2, 3])
        self.assertIsNone(nodes[0].parent_id)
        self.assertEqual(nodes[1].parent_id, nodes[0].node_id)
        self.assertEqual(nodes[2].parent_id, nodes[1].node_id)


if __name__ == "__main__":
    unittest.main()
