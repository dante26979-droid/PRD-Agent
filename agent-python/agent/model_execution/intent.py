from __future__ import annotations

from dataclasses import dataclass

from agent.context_pack import PreparedModelContext
from agent.model import ModelResponse


@dataclass(frozen=True)
class ModelCallIntent:
    operation: str
    operation_sequence: int
    operation_key: str
    prompt_version: str
    system_prompt: str
    max_output_tokens: int
    output_schema: str = "model-response.v1"
    base_request_hash: str | None = None


@dataclass(frozen=True)
class ModelExecutionResult:
    response: ModelResponse
    prepared_context: PreparedModelContext
    replayed: bool
