from __future__ import annotations

from threading import BoundedSemaphore
from time import monotonic, sleep
from typing import Callable

import httpx

from prd_agent.production.config import LlmSettings

from .errors import ModelApiError
from .models import ModelResponse


class DeepSeekChatClient:
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
        self._api_key = settings.api_key_file.read_text(
            encoding="utf-8"
        ).strip()
        timeout = httpx.Timeout(
            settings.timeout_seconds,
            connect=settings.connect_timeout_seconds,
            write=10.0,
            pool=5.0,
        )
        self._client = httpx.Client(
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=2,
                max_keepalive_connections=1,
            ),
            transport=transport,
        )

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        timeout_seconds: float,
    ) -> ModelResponse:
        acquired = self._capacity.acquire(
            timeout=self.settings.connect_timeout_seconds
        )
        if not acquired:
            raise ModelApiError(
                "local_capacity_exhausted",
                retryable=True,
            )
        try:
            return self._complete_unlocked(
                system_prompt,
                user_prompt,
                timeout_seconds=timeout_seconds,
            )
        finally:
            self._capacity.release()

    def _complete_unlocked(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        timeout_seconds: float,
    ) -> ModelResponse:
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
        response = None
        for attempt in range(self.settings.max_transport_retries + 1):
            try:
                response = self._client.post(
                    self.settings.base_url.rstrip("/")
                    + "/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request,
                    timeout=timeout_seconds,
                )
            except httpx.TransportError as exc:
                if attempt >= self.settings.max_transport_retries:
                    raise ModelApiError(
                        "transport_timeout"
                        if isinstance(exc, httpx.TimeoutException)
                        else "transport_connection",
                        retryable=True,
                    ) from exc
                self._sleeper(float(1 if attempt == 0 else 3))
                continue
            if response.status_code not in {
                408,
                429,
                500,
                502,
                503,
                504,
            } or attempt >= self.settings.max_transport_retries:
                break
            self._sleeper(self._retry_delay(response, attempt))
        assert response is not None
        if response.status_code >= 400:
            raise self._http_error(response.status_code)
        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            if content is None:
                content = ""
            if not isinstance(content, str):
                raise TypeError("model content must be text")
            model_id = str(
                payload.get("model") or self.settings.model
            )
            token_usage = dict(payload.get("usage") or {})
            finish_reason = str(
                choice.get("finish_reason") or "unknown"
            )
            provider_request_id = (
                response.headers.get("x-request-id")
                or (
                    str(payload["id"])
                    if payload.get("id")
                    else None
                )
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ModelApiError(
                "provider_protocol",
                retryable=False,
                status_code=response.status_code,
            ) from exc
        return ModelResponse(
            output=content,
            model_id=model_id,
            token_usage=token_usage,
            finish_reason=finish_reason,
            provider_request_id=provider_request_id,
            latency_ms=int((monotonic() - started) * 1000),
        )

    def close(self) -> None:
        self._client.close()

    def list_models(self) -> tuple[str, ...]:
        response = self._client.get(
            self.settings.base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self.settings.timeout_seconds,
        )
        if response.status_code >= 400:
            raise self._http_error(response.status_code)
        try:
            data = response.json()["data"]
            return tuple(
                str(item["id"])
                for item in data
                if isinstance(item, dict) and item.get("id")
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelApiError(
                "provider_protocol",
                retryable=False,
                status_code=response.status_code,
            ) from exc

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("retry-after", "")
        try:
            return min(max(float(value), 0.0), 30.0)
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
            retryable=status_code
            in {408, 429, 500, 502, 503, 504},
            status_code=status_code,
        )
