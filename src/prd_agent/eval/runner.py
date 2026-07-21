"""Evaluation runner and an in-memory store used by the offline test path."""

from __future__ import annotations

from typing import Protocol

from .models import BaselineConfig, BaselineRun, EvalDataset
from .prompt_baseline import DirectPromptBaseline, ModelAdapter


class RunStore(Protocol):
    def get(self, eval_run_id: str) -> BaselineRun | None:
        ...

    def save(self, run: BaselineRun) -> None:
        ...

    def all(self) -> list[BaselineRun]:
        ...


class InMemoryRunStore:
    """Deterministic store for unit tests and the offline Stub Model."""

    def __init__(self) -> None:
        self._runs: dict[str, BaselineRun] = {}

    def get(self, eval_run_id: str) -> BaselineRun | None:
        return self._runs.get(eval_run_id)

    def save(self, run: BaselineRun) -> None:
        existing = self._runs.get(run.eval_run_id)
        if existing and existing.input_hash != run.input_hash:
            raise ValueError(f"run id collision with different input: {run.eval_run_id}")
        self._runs[run.eval_run_id] = run

    def all(self) -> list[BaselineRun]:
        return list(self._runs.values())


class BaselineRunner:
    def __init__(
        self,
        dataset: EvalDataset,
        config: BaselineConfig,
        model: ModelAdapter,
        store: RunStore,
        baseline: DirectPromptBaseline | None = None,
    ) -> None:
        if config.dataset_version != dataset.dataset_version:
            raise ValueError("baseline config dataset_version does not match dataset")
        self.dataset = dataset
        self.config = config
        self.model = model
        self.store = store
        self.baseline = baseline or DirectPromptBaseline()

    def run(self) -> list[BaselineRun]:
        runs: list[BaselineRun] = []
        for case in self.dataset.cases:
            for trial_no in range(1, self.config.trials_per_case + 1):
                expected_input_hash = self.baseline.input_hash(case, self.config)
                # The deterministic run id is produced by the baseline before the call.
                # A completed record is safe to reuse; failures are retried on the next run.
                eval_run_id = self.baseline.run_id(case, self.config, trial_no)
                existing = self.store.get(eval_run_id)
                if existing and existing.status == "completed" and existing.input_hash == expected_input_hash:
                    runs.append(existing)
                    continue
                run = self.baseline.run_case(case, self.config, self.model, trial_no=trial_no)
                self.store.save(run)
                runs.append(run)
        return runs
