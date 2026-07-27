import unittest

from prd_agent.eval.minimal_workflow import MinimalWorkflowBaseline
from prd_agent.eval.models import BaselineConfig, EvalCase
from prd_agent.workflow.stub_model import HeuristicWorkflowModel


class MinimalWorkflowEvalTests(unittest.TestCase):
    def test_generates_markdown_through_confirmation_workflow(self) -> None:
        case = EvalCase.from_dict(
            {
                "case_id": "case-workflow",
                "title": "创建时间筛选",
                "requirement": "订单列表增加创建时间筛选",
                "context": "仅使用需求输入",
                "repository_id": "demo-repo",
                "resolved_commit_sha": "a" * 40,
                "required_prd_sections": ["requirement_summary"],
            }
        )
        config = BaselineConfig(
            "minimal-workflow-v1",
            "m0-step2-v1",
            "heuristic-workflow-model",
            "eval-v1",
            1,
        )

        run = MinimalWorkflowBaseline().run_case(
            case, config, HeuristicWorkflowModel(), trial_no=1
        )

        self.assertEqual(run.status, "completed")
        self.assertIn("## 第一确认单元", run.output)
        self.assertIn("## 待确认事项", run.output)


if __name__ == "__main__":
    unittest.main()
