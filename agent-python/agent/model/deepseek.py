from __future__ import annotations

from dataclasses import dataclass, field
from threading import BoundedSemaphore
from time import monotonic, sleep
from typing import Any, Callable, Mapping

import httpx

from agent.config import LlmSettings


class ModelApiError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(f"model API request failed: {code}")
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class ModelResponse:
    output: str
    token_usage: Mapping[str, Any] = field(default_factory=dict)
    model_id: str = "unknown"
    finish_reason: str = "stop"
    provider_request_id: str | None = None
    latency_ms: int = 0


class DeepSeekChatClient:
    """Bounded remote model Adapter used only inside the Python Agent."""

    def __init__(
        self,
        settings: LlmSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self.settings = settings
        self._sleeper = sleeper
        self._capacity = BoundedSemaphore(value=1)
        self._api_key = settings.api_key_file.read_text(encoding="utf-8").strip()
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                settings.timeout_seconds,
                connect=settings.connect_timeout_seconds,
                write=10.0,
                pool=5.0,
            ),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            transport=transport,
        )

    def complete(self, system_prompt: str, user_prompt: str) -> ModelResponse:
        if not self._capacity.acquire(timeout=self.settings.connect_timeout_seconds):
            raise ModelApiError("local_capacity_exhausted", retryable=True)
        try:
            return self._complete_unlocked(system_prompt, user_prompt)
        finally:
            self._capacity.release()

    def _complete_unlocked(self, system_prompt: str, user_prompt: str) -> ModelResponse:
        started = monotonic()
        request = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
            "max_tokens": self.settings.max_output_tokens,
            "thinking": {"type": "disabled"},
        }
        response: httpx.Response | None = None
        for attempt in range(self.settings.max_transport_retries + 1):
            try:
                response = self._client.post(
                    self.settings.base_url.rstrip("/") + "/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request,
                )
            except httpx.TransportError as error:
                if attempt >= self.settings.max_transport_retries:
                    code = (
                        "transport_timeout"
                        if isinstance(error, httpx.TimeoutException)
                        else "transport_connection"
                    )
                    raise ModelApiError(code, retryable=True) from error
                self._sleeper(float(1 if attempt == 0 else 3))
                continue
            if (
                response.status_code not in {408, 429, 500, 502, 503, 504}
                or attempt >= self.settings.max_transport_retries
            ):
                break
            self._sleeper(self._retry_delay(response, attempt))
        if response is None:
            raise ModelApiError("provider_protocol", retryable=True)
        if response.status_code >= 400:
            raise self._http_error(response.status_code)
        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("model content must be text")
            raw_token_usage = payload.get("usage") or {}
            if not isinstance(raw_token_usage, Mapping):
                raise TypeError("model token usage must be an object")
            # Provider-specific detail objects are not budget counters and are
            # deliberately excluded from the durable execution ledger. Keep
            # only bounded, non-negative integer counters with stable names.
            token_usage = {
                str(key): value
                for key, value in raw_token_usage.items()
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            }
            finish_reason = str(choice.get("finish_reason") or "unknown")
            provider_request_id = response.headers.get("x-request-id") or (
                str(payload["id"]) if payload.get("id") else None
            )
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise ModelApiError(
                "provider_protocol",
                retryable=False,
                status_code=response.status_code,
            ) from error
        return ModelResponse(
            output=content,
            token_usage=token_usage,
            model_id=str(payload.get("model") or self.settings.model),
            finish_reason=finish_reason,
            provider_request_id=provider_request_id,
            latency_ms=int((monotonic() - started) * 1000),
        )

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        try:
            return min(max(float(response.headers.get("retry-after", "")), 0.0), 30.0)
        except ValueError:
            return float(1 if attempt == 0 else 3)

    @staticmethod
    def _http_error(status_code: int) -> ModelApiError:
        if status_code == 401:
            code = "authentication"
        elif status_code == 402:
            code = "insufficient_balance"
        elif status_code == 403:
            code = "permission"
        elif status_code == 404:
            code = "model_or_endpoint"
        elif status_code in {400, 422}:
            code = "permanent_request"
        elif status_code in {408, 429, 500, 502, 503, 504}:
            code = "retry_exhausted"
        else:
            code = "provider_protocol"
        return ModelApiError(
            code,
            retryable=status_code in {408, 429, 500, 502, 503, 504},
            status_code=status_code,
        )
