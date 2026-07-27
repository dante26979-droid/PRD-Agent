from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class StructuredModelResult:
    model_id: str
    prompt_version: str
    structured_output: Mapping[str, Any]
    raw_output_hash: str
    token_usage: Mapping[str, Any] = field(default_factory=dict)
    finish_reason: str = "stop"


class WorkflowModel(Protocol):
    def complete(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        repair: bool = False,
    ) -> StructuredModelResult:
        """Return structured output. No tool schema is supplied in Step 2."""


class JsonWorkflowModelAdapter:
    """Bridge the Step 1 ``ModelAdapter`` contract to structured workflow calls."""

    def __init__(self, model, *, timeout_seconds: float = 120.0) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds

    def complete(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        repair: bool = False,
    ) -> StructuredModelResult:
        system_prompt = (
            "你是 PRD Workflow 的结构化节点。只返回一个 JSON 对象，不得调用工具，"
            "不得输出私有推理。操作：" + operation
        )
        if repair:
            system_prompt += "。上次输出未通过 Schema 校验；请基于完全相同输入修复格式。"
        response = self.model.complete(
            system_prompt,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            timeout_seconds=self.timeout_seconds,
        )
        try:
            structured = json.loads(response.output)
        except json.JSONDecodeError:
            structured = {}
        if not isinstance(structured, dict):
            structured = {}
        return StructuredModelResult(
            model_id=response.model_id,
            prompt_version=f"{operation}.m0.step2.v1",
            structured_output=structured,
            raw_output_hash="sha256:"
            + hashlib.sha256(response.output.encode("utf-8")).hexdigest(),
            token_usage=dict(response.token_usage),
            finish_reason="stop",
        )
