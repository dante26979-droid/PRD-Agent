from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ModelResponse:
    output: str
    token_usage: Mapping[str, Any] = field(default_factory=dict)
    model_id: str = "unknown"
    finish_reason: str = "stop"
    provider_request_id: str | None = None
    latency_ms: int = 0
