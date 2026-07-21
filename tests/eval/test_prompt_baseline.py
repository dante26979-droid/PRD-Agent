import unittest

from prd_agent.eval.models import BaselineConfig, EvalCase, ModelResponse
from prd_agent.eval.prompt_baseline import DirectPromptBaseline


def make_case() -> EvalCase:
    return EvalCase.from_dict(
        {
            "case_id": "case-001",
            "title": "字段校验规则变更",
            "requirement": "支持手机号格式校验",
            "context": "当前系统使用字符串校验器",
            "tags": ["required_investigation"],
            "repository_id": "demo-repo",
            "resolved_commit_sha": "a" * 40,
            "expected_information_needs": [],
            "required_sources": [],
            "expected_facts": [],
            "expected_unknowns": [],
            "expected_conflicts": [],
            "required_prd_sections": ["requirement_summary"],
            "clarification_questions": [],
        }
    )


class SpyModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str, *, timeout_seconds: float):
        self.calls.append((system_prompt, user_prompt))
        return ModelResponse(
            output="# 需求摘要\n\n支持手机号格式校验。",
            token_usage={"input": 10, "output": 8},
            model_id="spy-model",
        )


class DirectPromptBaselineTests(unittest.TestCase):
    def test_build_input_hash_is_stable(self) -> None:
        config = BaselineConfig(
            config_id="direct-prompt-v1",
            prompt_version="direct-prompt-v1",
            model_id="spy-model",
            dataset_version="eval-v1",
        )
        baseline = DirectPromptBaseline()

        first = baseline.input_hash(make_case(), config)
        second = baseline.input_hash(make_case(), config)

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("sha256:"))

    def test_calls_model_without_tool_schema(self) -> None:
        config = BaselineConfig(
            config_id="direct-prompt-v1",
            prompt_version="direct-prompt-v1",
            model_id="spy-model",
            dataset_version="eval-v1",
        )
        model = SpyModel()
        run = DirectPromptBaseline().run_case(make_case(), config, model, trial_no=1)

        self.assertEqual(run.status, "completed")
        self.assertEqual(len(model.calls), 1)
        system_prompt, user_prompt = model.calls[0]
        self.assertNotIn("tools", system_prompt.lower())
        self.assertNotIn("tool_schema", user_prompt.lower())
        self.assertEqual(run.model_id, "spy-model")
        self.assertIsNotNone(run.output_hash)

    def test_model_error_is_recorded_without_fake_output(self) -> None:
        class FailingModel:
            def complete(self, system_prompt: str, user_prompt: str, *, timeout_seconds: float):
                raise TimeoutError("model timed out")

        config = BaselineConfig(
            config_id="direct-prompt-v1",
            prompt_version="direct-prompt-v1",
            model_id="failing-model",
            dataset_version="eval-v1",
        )
        run = DirectPromptBaseline().run_case(make_case(), config, FailingModel(), trial_no=1)

        self.assertEqual(run.status, "failed")
        self.assertIsNone(run.output)
        self.assertEqual(run.error, "TimeoutError: model timed out")


if __name__ == "__main__":
    unittest.main()
