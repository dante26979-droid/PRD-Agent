from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from time import perf_counter
from typing import Any, Mapping

from agent.model import ModelResponse


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ModelObservation:
    sequence: int
    request_hash: str
    output_hash: str | None
    duration_ms: int
    status: str
    error_category: str | None = None


class ScriptedAgentModel:
    """Deterministic Agent model whose outputs are fixed fixture objects."""

    def __init__(
        self,
        outputs: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        *,
        model_id: str = "scripted-agent-model",
    ) -> None:
        self._outputs = [deepcopy(dict(item)) for item in outputs]
        self.model_id = model_id
        self.calls: list[dict[str, Any]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> ModelResponse:
        if not self._outputs:
            raise AssertionError("unexpected scripted model call")
        try:
            request = json.loads(user_prompt)
        except json.JSONDecodeError:
            request = {"request_hash": _hash(user_prompt)}
        self.calls.append(request)
        output = self._outputs.pop(0)
        tokens = int(output.pop("_tokens", 1))
        latency_ms = int(output.pop("_latency_ms", 0))
        invalid_json = output.pop("_invalid_json", None)
        raw = str(invalid_json) if invalid_json is not None else json.dumps(
            output, ensure_ascii=False, sort_keys=True
        )
        return ModelResponse(
            output=raw,
            token_usage={"total_tokens": tokens},
            model_id=self.model_id,
            latency_ms=latency_ms,
        )


class InstrumentedModel:
    """Observe physical model calls without retaining prompt or response text."""

    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.observations: list[ModelObservation] = []

    def complete(self, system_prompt: str, user_prompt: str) -> ModelResponse:
        sequence = len(self.observations) + 1
        request_hash = _hash(system_prompt + "\x00" + user_prompt)
        started = perf_counter()
        try:
            response = self.delegate.complete(system_prompt, user_prompt)
        except Exception as error:
            self.observations.append(
                ModelObservation(
                    sequence=sequence,
                    request_hash=request_hash,
                    output_hash=None,
                    duration_ms=int((perf_counter() - started) * 1000),
                    status="FAILED",
                    error_category=getattr(error, "code", type(error).__name__),
                )
            )
            raise
        self.observations.append(
            ModelObservation(
                sequence=sequence,
                request_hash=request_hash,
                output_hash=_hash(response.output),
                duration_ms=max(
                    int((perf_counter() - started) * 1000),
                    int(response.latency_ms or 0),
                ),
                status="SUCCEEDED",
            )
        )
        return response
