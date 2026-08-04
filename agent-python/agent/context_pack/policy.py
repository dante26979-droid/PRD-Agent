from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ContextPolicy:
    version: str = "context-policy.off"
    mode: str = "off"
    context_window_tokens: int = 64_000
    compact_threshold_tokens: int = 40_000
    target_input_tokens: int = 24_000
    reserved_output_tokens: int = 4_096
    emergency_margin_tokens: int = 2_048
    semantic_compaction_enabled: bool = False
    semantic_max_output_tokens: int = 2_048
    max_observations: int = 12
    max_prior_actions: int = 12
    max_optional_string_chars: int = 2_000

    def __post_init__(self) -> None:
        if self.mode not in {"off", "shadow", "enforce"}:
            raise ValueError("context policy mode must be off, shadow, or enforce")
        if not self.version or len(self.version) > 80:
            raise ValueError("context policy version must be bounded")
        numeric = (
            self.context_window_tokens,
            self.compact_threshold_tokens,
            self.target_input_tokens,
            self.reserved_output_tokens,
            self.emergency_margin_tokens,
            self.semantic_max_output_tokens,
        )
        if any(value < 0 for value in numeric):
            raise ValueError("context policy token values must be non-negative")
        if self.context_window_tokens <= 0:
            raise ValueError("context window must be positive")
        if self.target_input_tokens > self.compact_threshold_tokens:
            raise ValueError("context target cannot exceed compact threshold")
        if self.hard_input_tokens <= 0:
            raise ValueError("reserved tokens leave no model input capacity")
        if self.compact_threshold_tokens > self.hard_input_tokens:
            raise ValueError("compact threshold exceeds hard model input capacity")
        if self.target_input_tokens > self.hard_input_tokens:
            raise ValueError("context target exceeds hard model input capacity")
        if any(
            value <= 0
            for value in (
                self.max_observations,
                self.max_prior_actions,
                self.max_optional_string_chars,
            )
        ):
            raise ValueError("context collection limits must be positive")

    @property
    def hard_input_tokens(self) -> int:
        return (
            self.context_window_tokens
            - self.reserved_output_tokens
            - self.emergency_margin_tokens
        )

    @classmethod
    def from_environment(cls) -> "ContextPolicy":
        mode = os.getenv("PRD_AGENT_CONTEXT_POLICY_MODE", "off").strip().lower()
        version = os.getenv(
            "PRD_AGENT_CONTEXT_POLICY_VERSION",
            "context-policy.off" if mode == "off" else "context-policy.v1",
        ).strip()
        return cls(
            version=version,
            mode=mode,
            context_window_tokens=_env_int("PRD_AGENT_CONTEXT_WINDOW_TOKENS", 64_000),
            compact_threshold_tokens=_env_int(
                "PRD_AGENT_CONTEXT_COMPACT_THRESHOLD_TOKENS", 40_000
            ),
            target_input_tokens=_env_int(
                "PRD_AGENT_CONTEXT_TARGET_INPUT_TOKENS", 24_000
            ),
            reserved_output_tokens=_env_int(
                "PRD_AGENT_CONTEXT_RESERVED_OUTPUT_TOKENS", 4_096
            ),
            emergency_margin_tokens=_env_int(
                "PRD_AGENT_CONTEXT_EMERGENCY_MARGIN_TOKENS", 2_048
            ),
            semantic_compaction_enabled=_env_bool(
                "PRD_AGENT_CONTEXT_SEMANTIC_COMPACTION", False
            ),
            semantic_max_output_tokens=_env_int(
                "PRD_AGENT_CONTEXT_COMPACTOR_MAX_OUTPUT_TOKENS", 2_048
            ),
            max_observations=_env_int("PRD_AGENT_CONTEXT_MAX_OBSERVATIONS", 12),
            max_prior_actions=_env_int("PRD_AGENT_CONTEXT_MAX_PRIOR_ACTIONS", 12),
            max_optional_string_chars=_env_int(
                "PRD_AGENT_CONTEXT_MAX_OPTIONAL_STRING_CHARS", 2_000
            ),
        )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")
