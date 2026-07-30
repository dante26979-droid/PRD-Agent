from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
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
    timeout_seconds: float = 90.0
    connect_timeout_seconds: float = 5.0
    max_output_tokens: int = 4096
    max_iterations: int = 8
    run_token_budget: int = 24000
    max_transport_retries: int = 2


def deployment_environment() -> DeploymentEnvironment:
    raw = os.environ.get("PRD_AGENT_ENVIRONMENT")
    value = "local" if raw is None else raw.strip().lower()
    try:
        return DeploymentEnvironment(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in DeploymentEnvironment)
        raise RuntimeError(
            f"PRD_AGENT_ENVIRONMENT must be one of: {allowed}"
        ) from exc


def cors_origins(
    environment: DeploymentEnvironment,
) -> tuple[str, ...]:
    raw = os.environ.get("PRD_AGENT_CORS_ORIGINS", "")
    if not raw and environment is DeploymentEnvironment.LOCAL:
        return (
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        )
    values = tuple(
        item.strip().rstrip("/")
        for item in raw.split(",")
        if item.strip()
    )
    if not values:
        raise RuntimeError(
            "PRD_AGENT_CORS_ORIGINS requires at least one explicit origin"
        )
    for value in values:
        parsed = urlsplit(value)
        unsafe = (
            value == "*"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query != ""
            or parsed.fragment != ""
            or parsed.path not in {"", "/"}
        )
        if environment is not DeploymentEnvironment.LOCAL:
            unsafe = unsafe or parsed.scheme != "https"
            unsafe = unsafe or parsed.hostname in {
                "localhost",
                "127.0.0.1",
                "::1",
            }
        elif parsed.scheme not in {"http", "https"}:
            unsafe = True
        if unsafe:
            raise RuntimeError(
                "PRD_AGENT_CORS_ORIGINS contains an unsafe origin"
            )
    return values


def secret_or_environment(name: str) -> str:
    file_name = os.environ.get(f"{name}_FILE")
    if file_name:
        value = Path(file_name).read_text(encoding="utf-8").strip()
    else:
        value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} or {name}_FILE is required")
    return value


def _bounded_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _bounded_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def llm_settings(
    environment: DeploymentEnvironment,
) -> LlmSettings | None:
    provider = os.environ.get("PRD_AGENT_LLM_PROVIDER", "").strip().lower()
    if not provider:
        if environment in {
            DeploymentEnvironment.STAGING,
            DeploymentEnvironment.PRODUCTION,
        }:
            raise RuntimeError("PRD_AGENT_LLM_PROVIDER is required")
        return None
    if provider != "deepseek":
        raise RuntimeError("PRD_AGENT_LLM_PROVIDER must be deepseek")

    base_url = os.environ.get("PRD_AGENT_LLM_BASE_URL", "").strip()
    model = os.environ.get("PRD_AGENT_LLM_MODEL", "").strip()
    key_file_value = os.environ.get(
        "PRD_AGENT_LLM_API_KEY_FILE", ""
    ).strip()
    if not base_url:
        raise RuntimeError("PRD_AGENT_LLM_BASE_URL is required")
    if not model:
        raise RuntimeError("PRD_AGENT_LLM_MODEL is required")
    if not key_file_value:
        raise RuntimeError("PRD_AGENT_LLM_API_KEY_FILE is required")
    parsed_url = urlsplit(base_url)
    allowed_hosts = {
        item.strip().lower()
        for item in os.environ.get(
            "PRD_AGENT_LLM_ALLOWED_HOSTS", "api.deepseek.com"
        ).split(",")
        if item.strip()
    }
    unsafe_url = (
        parsed_url.scheme != "https"
        or parsed_url.hostname not in allowed_hosts
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query != ""
        or parsed_url.fragment != ""
        or parsed_url.path not in {"", "/"}
    )
    if unsafe_url:
        raise RuntimeError("PRD_AGENT_LLM_BASE_URL is unsafe")
    key_file = Path(key_file_value)
    try:
        has_key = bool(key_file.read_text(encoding="utf-8").strip())
    except OSError as exc:
        raise RuntimeError(
            "PRD_AGENT_LLM_API_KEY_FILE cannot be read"
        ) from exc
    if not has_key:
        raise RuntimeError("PRD_AGENT_LLM_API_KEY_FILE is empty")
    return LlmSettings(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key_file=key_file,
        timeout_seconds=_bounded_float(
            "PRD_AGENT_LLM_TIMEOUT_SECONDS",
            90.0,
            minimum=5.0,
            maximum=300.0,
        ),
        connect_timeout_seconds=_bounded_float(
            "PRD_AGENT_LLM_CONNECT_TIMEOUT_SECONDS",
            5.0,
            minimum=1.0,
            maximum=30.0,
        ),
        max_output_tokens=_bounded_int(
            "PRD_AGENT_LLM_MAX_OUTPUT_TOKENS",
            4096,
            minimum=64,
            maximum=131072,
        ),
        max_iterations=_bounded_int(
            "PRD_AGENT_LLM_MAX_ITERATIONS",
            8,
            minimum=1,
            maximum=50,
        ),
        run_token_budget=_bounded_int(
            "PRD_AGENT_LLM_RUN_TOKEN_BUDGET",
            24000,
            minimum=100,
            maximum=10_000_000,
        ),
        max_transport_retries=_bounded_int(
            "PRD_AGENT_LLM_MAX_TRANSPORT_RETRIES",
            2,
            minimum=0,
            maximum=5,
        ),
    )
