import unittest
from pathlib import Path

from prd_agent.eval.models import BaselineConfig, EvalCase
from prd_agent.eval.single_retrieval import SingleRetrievalBaseline
from prd_agent.workflow.stub_model import HeuristicWorkflowModel


class SingleRetrievalEvalTests(unittest.TestCase):
    def test_runs_fixed_repository_action_and_includes_evidence_in_prd(self) -> None:
        case = EvalCase.from_dict(
            {
                "case_id": "case-007",
                "title": "数据库金额约束",
                "requirement": "数据库必须拒绝金额小于等于 0 的订单。",
                "context": "需要核查数据库 Schema",
                "repository_id": "demo-repo",
                "resolved_commit_sha": "9eb1f3b0d0b8cdd9aa0252e3a6c24d3e3a5f4233",
                "required_prd_sections": ["requirement_summary"],
            }
        )
        config = BaselineConfig(
            "single-retrieval-v1",
            "single-retrieval-v1",
            "heuristic-workflow-model",
            "eval-v1",
            1,
        )

        run = SingleRetrievalBaseline(Path.cwd()).run_case(
            case, config, HeuristicWorkflowModel(), trial_no=1
        )

        self.assertEqual(run.status, "completed")
        self.assertIn("db/schema.sql", run.output)
        self.assertIn("DECIMAL(12, 2)", run.output)


if __name__ == "__main__":
    unittest.main()
