import unittest

from prd_agent.eval.models import BaselineConfig, EvalCase, EvalDataset, ModelResponse
from prd_agent.eval.prompt_baseline import DirectPromptBaseline
from prd_agent.eval.runner import BaselineRunner, InMemoryRunStore


class CountingModel:
    model_id = "counting-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system_prompt: str, user_prompt: str, *, timeout_seconds: float):
        self.calls += 1
        return ModelResponse(output="# 需求摘要\n\n完成。", model_id=self.model_id)


def case(case_id: str) -> EvalCase:
    return EvalCase.from_dict(
        {
            "case_id": case_id,
            "title": case_id,
            "requirement": "完成需求",
            "context": "固定上下文",
            "repository_id": "demo-repo",
            "resolved_commit_sha": "a" * 40,
            "required_prd_sections": ["requirement_summary"],
        }
    )


class BaselineRunnerTests(unittest.TestCase):
    def test_runs_every_case_and_trial(self) -> None:
        dataset = EvalDataset("eval-v1", "demo-repo", "a" * 40, (case("one"), case("two")))
        config = BaselineConfig("direct-v1", "prompt-v1", "counting-model", "eval-v1", 2)
        model = CountingModel()
        runs = BaselineRunner(dataset, config, model, InMemoryRunStore()).run()

        self.assertEqual(len(runs), 4)
        self.assertEqual(model.calls, 4)
        self.assertTrue(all(run.status == "completed" for run in runs))

    def test_completed_runs_are_idempotently_reused(self) -> None:
        dataset = EvalDataset("eval-v1", "demo-repo", "a" * 40, (case("one"),))
        config = BaselineConfig("direct-v1", "prompt-v1", "counting-model", "eval-v1", 2)
        model = CountingModel()
        store = InMemoryRunStore()
        runner = BaselineRunner(dataset, config, model, store)

        first = runner.run()
        second = runner.run()

        self.assertEqual(model.calls, 2)
        self.assertEqual([item.eval_run_id for item in first], [item.eval_run_id for item in second])
        self.assertEqual(len(store.all()), 2)


if __name__ == "__main__":
    unittest.main()
