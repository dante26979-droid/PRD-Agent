import unittest

from prd_agent.eval.metrics import evaluate_case
from prd_agent.eval.models import EvalCase


def metric_value(results, name):
    return next(item for item in results if item.name == name)


def make_case() -> EvalCase:
    return EvalCase.from_dict(
        {
            "case_id": "case-001",
            "title": "手机号校验",
            "requirement": "支持手机号格式校验",
            "context": "当前系统使用字符串校验器",
            "repository_id": "demo-repo",
            "resolved_commit_sha": "a" * 40,
            "required_prd_sections": [
                "requirement_summary",
                "scope_boundary",
                "acceptance_criteria",
                "open_questions",
            ],
            "expected_keywords": ["手机号", "格式校验"],
            "expected_unknowns": ["历史数据是否回填"],
        }
    )


class MetricsTests(unittest.TestCase):
    def test_scores_complete_output(self) -> None:
        output = """# 需求摘要
支持手机号格式校验。

## 范围边界
范围内：新增校验。范围外：历史数据回填，待确认。

## 验收标准
当手机号为空时，系统应拒绝请求并返回错误提示。

## 待确认事项
历史数据是否回填。
"""

        results = evaluate_case(make_case(), output)

        self.assertEqual(metric_value(results, "required_section_coverage").value, 1.0)
        self.assertEqual(metric_value(results, "requirement_coverage").value, 1.0)
        self.assertEqual(metric_value(results, "unknown_preservation").value, 1.0)
        self.assertEqual(metric_value(results, "acceptance_criteria_executability").value, 1.0)

    def test_reports_missing_sections_and_keywords(self) -> None:
        results = evaluate_case(make_case(), "# 需求摘要\n只描述了手机号。")

        self.assertEqual(metric_value(results, "required_section_coverage").value, 0.25)
        self.assertEqual(metric_value(results, "requirement_coverage").value, 0.5)
        self.assertEqual(metric_value(results, "unknown_preservation").value, 0.0)

    def test_marks_grounding_metrics_not_applicable_for_baseline(self) -> None:
        results = evaluate_case(make_case(), "# 需求摘要\n内容")

        grounding = metric_value(results, "unsupported_claim_rate")
        self.assertIsNone(grounding.value)
        self.assertEqual(grounding.status, "not_applicable")


if __name__ == "__main__":
    unittest.main()
