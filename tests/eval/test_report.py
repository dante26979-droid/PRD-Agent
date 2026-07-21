import unittest

from prd_agent.eval.models import BaselineConfig, EvalCase, EvalDataset, ModelResponse
from prd_agent.eval.prompt_baseline import DirectPromptBaseline
from prd_agent.eval.report import build_report


class ReportModel:
    def complete(self, system_prompt: str, user_prompt: str, *, timeout_seconds: float):
        return ModelResponse(
            output=(
                "# 需求摘要\n手机号格式校验\n\n"
                "## 范围边界\n范围内：新增校验。\n\n"
                "## 验收标准\n当输入为空时，系统应拒绝请求。\n\n"
                "## 待确认事项\n无。"
            ),
            model_id="report-model",
            token_usage={"total": 20},
        )


class ReportTests(unittest.TestCase):
    def test_report_contains_real_run_and_metric_summary(self) -> None:
        case = EvalCase.from_dict(
            {
                "case_id": "case-001",
                "title": "手机号校验",
                "requirement": "支持手机号格式校验",
                "context": "固定上下文",
                "repository_id": "demo-repo",
                "resolved_commit_sha": "a" * 40,
                "required_prd_sections": [
                    "requirement_summary",
                    "scope_boundary",
                    "acceptance_criteria",
                    "open_questions",
                ],
                "expected_keywords": ["手机号"],
            }
        )
        dataset = EvalDataset("eval-v1", "demo-repo", "a" * 40, (case,))
        config = BaselineConfig("direct-v1", "prompt-v1", "report-model", "eval-v1", 1)
        run = DirectPromptBaseline().run_case(case, config, ReportModel(), trial_no=1)

        report = build_report(dataset, config, [run])
        markdown = report.to_markdown()

        self.assertIn("eval-v1", markdown)
        self.assertIn("case-001", markdown)
        self.assertIn("required_section_coverage", markdown)
        self.assertIn("1.0", markdown)
        self.assertEqual(report.failed_runs, 0)
        self.assertEqual(report.completed_runs, 1)

    def test_failed_run_is_listed_without_fake_metrics(self) -> None:
        case = EvalCase.from_dict(
            {
                "case_id": "case-001",
                "title": "失败案例",
                "requirement": "需求",
                "context": "上下文",
                "repository_id": "demo-repo",
                "resolved_commit_sha": "a" * 40,
                "required_prd_sections": ["requirement_summary"],
            }
        )
        dataset = EvalDataset("eval-v1", "demo-repo", "a" * 40, (case,))
        config = BaselineConfig("direct-v1", "prompt-v1", "model", "eval-v1", 1)
        run = DirectPromptBaseline().run_case(
            case,
            config,
            type("FailingModel", (), {"complete": lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("timeout"))})(),
            trial_no=1,
        )

        report = build_report(dataset, config, [run])

        self.assertEqual(report.failed_runs, 1)
        self.assertIn("timeout", report.to_markdown())
        self.assertNotIn("required_section_coverage | 0", report.to_markdown())


if __name__ == "__main__":
    unittest.main()
