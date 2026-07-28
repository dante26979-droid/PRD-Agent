from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit


class DeploymentEnvironment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass(frozen=True)
class LlmSettings:
    provider: str
    base_url: str
    model: str
    api_key_file: Path
    timeout_seconds: float
    connect_timeout_seconds: float
    max_output_tokens: int
    max_iterations: int
    run_token_budget: int
    max_transport_retries: int


@dataclass(frozen=True)
class AgentSettings:
    environment: DeploymentEnvironment
    llm: LlmSettings | None
    capability_target: str | None
    max_checkpoint_bytes: int
    max_draft_bytes: int
    max_evidence_items: int

    @classmethod
    def load(cls) -> "AgentSettings":
        environment = _environment()
        return cls(
            environment=environment,
            llm=_llm_settings(environment),
            capability_target=_optional("PRD_AGENT_CAPABILITY_GATEWAY_TARGET"),
            max_checkpoint_bytes=_bounded_int(
                "PRD_AGENT_AGENT_MAX_CHECKPOINT_BYTES",
                256 * 1024,
                minimum=1024,
                maximum=4 * 1024 * 1024,
            ),
            max_draft_bytes=_bounded_int(
                "PRD_AGENT_AGENT_MAX_DRAFT_BYTES",
                1024 * 1024,
                minimum=1024,
                maximum=8 * 1024 * 1024,
            ),
            max_evidence_items=_bounded_int(
                "PRD_AGENT_AGENT_MAX_EVIDENCE_ITEMS",
                100,
                minimum=1,
                maximum=1000,
            ),
        )


def _environment() -> DeploymentEnvironment:
    value = os.getenv("PRD_AGENT_ENVIRONMENT", "local").strip().lower()
    try:
        return DeploymentEnvironment(value)
    except ValueError as error:
        allowed = ", ".join(item.value for item in DeploymentEnvironment)
        raise RuntimeError(f"PRD_AGENT_ENVIRONMENT must be one of: {allowed}") from error


def _llm_settings(environment: DeploymentEnvironment) -> LlmSettings | None:
    provider = os.getenv("PRD_AGENT_LLM_PROVIDER", "").strip().lower()
    if not provider:
        if environment in {DeploymentEnvironment.STAGING, DeploymentEnvironment.PRODUCTION}:
            raise RuntimeError("PRD_AGENT_LLM_PROVIDER is required")
        return None
    if provider != "deepseek":
        raise RuntimeError("PRD_AGENT_LLM_PROVIDER must be deepseek")
    base_url = _required("PRD_AGENT_LLM_BASE_URL")
    model = _required("PRD_AGENT_LLM_MODEL")
    key_file = Path(_required("PRD_AGENT_LLM_API_KEY_FILE"))
    allowed_hosts = {
        item.strip().lower()
        for item in os.getenv("PRD_AGENT_LLM_ALLOWED_HOSTS", "api.deepseek.com").split(",")
        if item.strip()
    }
    parsed = urlsplit(base_url)
    unsafe = (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query != ""
        or parsed.fragment != ""
        or parsed.path not in {"", "/"}
    )
    if unsafe:
        raise RuntimeError("PRD_AGENT_LLM_BASE_URL is unsafe")
    try:
        if not key_file.read_text(encoding="utf-8").strip():
            raise RuntimeError("PRD_AGENT_LLM_API_KEY_FILE is empty")
    except OSError as error:
        raise RuntimeError("PRD_AGENT_LLM_API_KEY_FILE cannot be read") from error
    return LlmSettings(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key_file=key_file,
        timeout_seconds=_bounded_float(
            "PRD_AGENT_LLM_TIMEOUT_SECONDS", 90.0, minimum=5.0, maximum=300.0
        ),
        connect_timeout_seconds=_bounded_float(
            "PRD_AGENT_LLM_CONNECT_TIMEOUT_SECONDS", 5.0, minimum=1.0, maximum=30.0
        ),
        max_output_tokens=_bounded_int(
            "PRD_AGENT_LLM_MAX_OUTPUT_TOKENS", 4096, minimum=64, maximum=131072
        ),
        max_iterations=_bounded_int(
            "PRD_AGENT_LLM_MAX_ITERATIONS", 8, minimum=1, maximum=50
        ),
        run_token_budget=_bounded_int(
            "PRD_AGENT_LLM_RUN_TOKEN_BUDGET",
            24000,
            minimum=100,
            maximum=10_000_000,
        ),
        max_transport_retries=_bounded_int(
            "PRD_AGENT_LLM_MAX_TRANSPORT_RETRIES", 2, minimum=0, maximum=5
        ),
    )


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number") from error
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value
