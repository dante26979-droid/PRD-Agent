from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import Any, Mapping, Protocol

from .prompts import structured_system_prompt


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
        system_prompt = structured_system_prompt(
            operation,
            repair=repair,
        )
        response = self.model.complete(
            system_prompt,
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                default=_json_value,
            ),
            timeout_seconds=self.timeout_seconds,
        )
        if response.finish_reason != "stop":
            structured = {}
        else:
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
            finish_reason=response.finish_reason,
        )


def _json_value(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    raise TypeError(
        f"unsupported model payload value: {type(value).__name__}"
    )
