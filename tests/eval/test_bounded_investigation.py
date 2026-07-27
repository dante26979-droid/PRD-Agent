import unittest
from pathlib import Path

from prd_agent.eval.bounded_investigation import BoundedInvestigationBaseline
from prd_agent.eval.models import BaselineConfig, EvalCase
from prd_agent.workflow.stub_model import HeuristicWorkflowModel


class BoundedInvestigationEvalTests(unittest.TestCase):
    def test_runs_bounded_loop_and_records_operational_metrics(self) -> None:
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
            "bounded-investigation-v1",
            "information-need-v1+action-selector-v1",
            "scripted-investigation-model",
            "eval-v1",
            1,
        )

        run = BoundedInvestigationBaseline(Path.cwd()).run_case(
            case, config, HeuristicWorkflowModel(), trial_no=1
        )

        self.assertEqual(run.status, "completed", run.error)
        self.assertIn("db/schema.sql", run.output)
        self.assertEqual(run.metadata["coverage_completion_rate"], 1.0)
        self.assertEqual(run.metadata["tool_call_count"], 1)
        self.assertEqual(run.metadata["stop_reason"], "COVERAGE_COMPLETE")


if __name__ == "__main__":
    unittest.main()
