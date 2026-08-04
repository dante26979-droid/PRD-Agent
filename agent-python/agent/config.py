from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from agent.context_pack import ContextPolicy
from agent.project_memory import ProjectMemoryPolicy


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
    max_tool_calls: int
    no_progress_limit: int
    max_replans: int
    run_token_budget: int
    max_transport_retries: int
    max_supplements: int = 1
    max_quality_repairs: int = 1


@dataclass(frozen=True)
class AgentSettings:
    environment: DeploymentEnvironment
    loop_mode: str
    advanced_loop_mode: str
    llm: LlmSettings | None
    capability_target: str | None
    service_token: str | None
    max_checkpoint_bytes: int
    max_draft_bytes: int
    max_run_artifact_bytes: int
    max_evidence_items: int
    event_ack_timeout_seconds: int
    context_policy: ContextPolicy
    project_memory_policy: ProjectMemoryPolicy = ProjectMemoryPolicy()

    @classmethod
    def load(cls, *, require_service_identity: bool = False) -> "AgentSettings":
        environment = _environment()
        loop_mode = _loop_mode()
        context_policy = ContextPolicy.from_environment()
        project_memory_policy = ProjectMemoryPolicy.from_environment()
        if loop_mode != "langgraph" and context_policy.mode != "off":
            raise RuntimeError(
                "context compression requires PRD_AGENT_AGENT_LOOP_MODE=langgraph"
            )
        if loop_mode != "langgraph" and project_memory_policy.mode != "off":
            raise RuntimeError(
                "project memory requires PRD_AGENT_AGENT_LOOP_MODE=langgraph"
            )
        return cls(
            environment=environment,
            loop_mode=loop_mode,
            advanced_loop_mode=_advanced_loop_mode(),
            llm=_llm_settings(environment),
            capability_target=_optional("PRD_AGENT_CAPABILITY_GATEWAY_TARGET"),
            service_token=_service_token(
                environment,
                required=require_service_identity,
            ),
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
            max_run_artifact_bytes=_bounded_int(
                "PRD_AGENT_AGENT_MAX_RUN_ARTIFACT_BYTES",
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
            event_ack_timeout_seconds=_bounded_int(
                "PRD_AGENT_AGENT_EVENT_ACK_TIMEOUT_SECONDS",
                30,
                minimum=1,
                maximum=300,
            ),
            context_policy=context_policy,
            project_memory_policy=project_memory_policy,
        )


def _loop_mode() -> str:
    value = os.getenv("PRD_AGENT_AGENT_LOOP_MODE", "langgraph").strip().lower()
    if value not in {"langgraph", "legacy"}:
        raise RuntimeError(
            "PRD_AGENT_AGENT_LOOP_MODE must be one of: langgraph, legacy"
        )
    return value


def _advanced_loop_mode() -> str:
    value = os.getenv("PRD_AGENT_ADVANCED_LOOP_MODE", "off").strip().lower()
    if value not in {"off", "shadow", "enforce"}:
        raise RuntimeError(
            "PRD_AGENT_ADVANCED_LOOP_MODE must be one of: off, shadow, enforce"
        )
    return value


def _service_token(
    environment: DeploymentEnvironment,
    *,
    required: bool,
) -> str | None:
    path_value = _optional("PRD_AGENT_AGENT_RPC_TOKEN_FILE")
    direct_value = _optional("PRD_AGENT_AGENT_RPC_TOKEN")
    if path_value is None:
        if direct_value is not None and environment in {
            DeploymentEnvironment.LOCAL,
            DeploymentEnvironment.TEST,
        }:
            if len(direct_value) < 32:
                raise RuntimeError(
                    "PRD_AGENT_AGENT_RPC_TOKEN must contain at least 32 characters"
                )
            return direct_value
        if required:
            raise RuntimeError("PRD_AGENT_AGENT_RPC_TOKEN_FILE is required")
        return None
    path = Path(path_value)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError("PRD_AGENT_AGENT_RPC_TOKEN_FILE cannot be read") from error
    if len(token) < 32:
        raise RuntimeError(
            "PRD_AGENT_AGENT_RPC_TOKEN_FILE must contain at least 32 characters"
        )
    return token


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
        max_tool_calls=_bounded_int(
            "PRD_AGENT_LLM_MAX_TOOL_CALLS", 8, minimum=1, maximum=50
        ),
        no_progress_limit=_bounded_int(
            "PRD_AGENT_LLM_NO_PROGRESS_LIMIT", 2, minimum=1, maximum=5
        ),
        max_replans=_bounded_int(
            "PRD_AGENT_LLM_MAX_REPLANS", 1, minimum=0, maximum=3
        ),
        max_supplements=_bounded_int(
            "PRD_AGENT_LLM_MAX_SUPPLEMENTS", 1, minimum=0, maximum=2
        ),
        max_quality_repairs=_bounded_int(
            "PRD_AGENT_LLM_MAX_QUALITY_REPAIRS", 1, minimum=0, maximum=2
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
