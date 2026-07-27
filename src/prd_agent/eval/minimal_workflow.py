"""Minimal Workflow ablation built on the real Step 2 public service API."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import time

from prd_agent.domain.commands import ConfirmOutline, ConfirmUnit, StartTask
from prd_agent.hashing import sha256_json
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService

from .models import BaselineConfig, BaselineRun, EvalCase


class MinimalWorkflowBaseline:
    graph_version = "m0.step2.v1"

    def input_hash(self, case: EvalCase, config: BaselineConfig) -> str:
        return sha256_json(
            {
                "case": case.prompt_payload(),
                "graph_version": self.graph_version,
                "prompt_version": config.prompt_version,
                "dataset_version": config.dataset_version,
            }
        )

    def run_id(self, case: EvalCase, config: BaselineConfig, trial_no: int) -> str:
        return "run-" + hashlib.sha256(
            f"{config.config_id}:{case.case_id}:{trial_no}:{self.input_hash(case, config)}".encode()
        ).hexdigest()[:24]

    def run_case(self, case, config, model, *, trial_no: int) -> BaselineRun:
        input_hash = self.input_hash(case, config)
        run_id = self.run_id(case, config, trial_no)
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        try:
            service = WorkflowService(InMemoryWorkflowRepository(), model)
            outline_wait = service.start_task(
                StartTask(
                    f"{case.requirement}\n\n已知上下文：{case.context}",
                    f"eval-{run_id}-start",
                )
            )
            if not outline_wait.current_outline:
                raise ValueError("eval case requires clarification before outline generation")
            unit_wait = service.confirm_outline(
                ConfirmOutline(
                    outline_wait.task.task_id,
                    outline_wait.current_outline.version,
                    outline_wait.task.version,
                    f"eval-{run_id}-outline",
                )
            )
            unit = unit_wait.current_outline.confirmation_units[0]
            final = service.confirm_unit(
                ConfirmUnit(
                    unit_wait.task.task_id,
                    unit.unit_id,
                    unit_wait.task.version,
                    f"eval-{run_id}-unit",
                )
            )
            output = final.markdown or ""
        except Exception as exc:  # noqa: BLE001 - failures are eval data
            return BaselineRun(
                eval_run_id=run_id,
                case_id=case.case_id,
                config_id=config.config_id,
                trial_no=trial_no,
                model_id=config.model_id,
                prompt_version=config.prompt_version,
                dataset_version=config.dataset_version,
                repository_commit=case.resolved_commit_sha,
                input_hash=input_hash,
                output_hash=None,
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started) * 1000),
                token_usage={},
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        return BaselineRun(
            eval_run_id=run_id,
            case_id=case.case_id,
            config_id=config.config_id,
            trial_no=trial_no,
            model_id=getattr(model, "model_id", config.model_id),
            prompt_version=config.prompt_version,
            dataset_version=config.dataset_version,
            repository_commit=case.resolved_commit_sha,
            input_hash=input_hash,
            output_hash="sha256:" + hashlib.sha256(output.encode()).hexdigest(),
            started_at=started_at,
            duration_ms=int((time.perf_counter() - started) * 1000),
            token_usage={},
            status="completed",
            output=output,
        )
