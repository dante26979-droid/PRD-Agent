"""Direct Prompt baseline adapter.

The baseline deliberately has one narrow public operation: submit a normalized
case to a model without registering tools or retrieval providers.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import time
from typing import Protocol

from .models import BaselineConfig, BaselineRun, EvalCase, ModelResponse, sha256_json


class ModelAdapter(Protocol):
    def complete(
        self, system_prompt: str, user_prompt: str, *, timeout_seconds: float
    ) -> ModelResponse:
        """Return one model response for the supplied prompt."""


class DirectPromptBaseline:
    """Run the fixed no-tool/no-retrieval baseline."""

    system_prompt = (
        "你是 PRD 分析助手。仅基于输入需求和上下文撰写结构化 PRD。"
        "不得编造当前系统事实；信息不足时必须写入待确认事项。"
        "输出必须包含需求摘要、范围边界、业务规则、流程、验收标准、异常和待确认事项。"
    )

    def build_user_prompt(self, case: EvalCase) -> str:
        payload = case.prompt_payload()
        return (
            "请根据以下固定需求输入生成一份可评审的 PRD。\n"
            "只使用提供的内容，不进行外部检索。\n\n"
            f"需求输入：\n{payload['requirement']}\n\n"
            f"已知上下文：\n{payload['context']}\n\n"
            "请使用 Markdown 输出，并明确标注假设、范围外内容和待确认事项。"
        )

    def input_hash(self, case: EvalCase, config: BaselineConfig) -> str:
        return sha256_json(
            {
                "case": case.prompt_payload(),
                "system_prompt": self.system_prompt,
                "prompt_version": config.prompt_version,
                "dataset_version": config.dataset_version,
            }
        )

    def run_id(self, case: EvalCase, config: BaselineConfig, trial_no: int) -> str:
        if trial_no < 1:
            raise ValueError("trial_no must be positive")
        input_hash = self.input_hash(case, config)
        return "run-" + hashlib.sha256(
            f"{config.config_id}:{case.case_id}:{trial_no}:{input_hash}".encode("utf-8")
        ).hexdigest()[:24]

    def run_case(
        self,
        case: EvalCase,
        config: BaselineConfig,
        model: ModelAdapter,
        *,
        trial_no: int,
    ) -> BaselineRun:
        if trial_no < 1:
            raise ValueError("trial_no must be positive")
        input_hash = self.input_hash(case, config)
        eval_run_id = self.run_id(case, config, trial_no)
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        try:
            response = model.complete(
                self.system_prompt,
                self.build_user_prompt(case),
                timeout_seconds=config.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - preserve provider failures as Run data
            return BaselineRun(
                eval_run_id=eval_run_id,
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

        output_hash = "sha256:" + hashlib.sha256(response.output.encode("utf-8")).hexdigest()
        return BaselineRun(
            eval_run_id=eval_run_id,
            case_id=case.case_id,
            config_id=config.config_id,
            trial_no=trial_no,
            model_id=response.model_id or config.model_id,
            prompt_version=config.prompt_version,
            dataset_version=config.dataset_version,
            repository_commit=case.resolved_commit_sha,
            input_hash=input_hash,
            output_hash=output_hash,
            started_at=started_at,
            duration_ms=int((time.perf_counter() - started) * 1000),
            token_usage=dict(response.token_usage),
            status="completed",
            output=response.output,
        )
