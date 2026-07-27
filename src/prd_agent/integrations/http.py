from __future__ import annotations

from dataclasses import dataclass
import json
import socket
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .errors import IntegrationError, IntegrationErrorCode


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: Any


class JsonHttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        timeout_seconds: float = 10,
    ) -> HttpResponse: ...


class UrllibJsonHttpTransport:
    def __init__(self, *, max_response_bytes: int = 8 * 1024 * 1024) -> None:
        self.max_response_bytes = max_response_bytes

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        timeout_seconds: float = 10,
    ) -> HttpResponse:
        body = None
        request_headers = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise IntegrationError(
                        IntegrationErrorCode.PAYLOAD_TOO_LARGE,
                        "provider response exceeded the allowed size",
                    )
                return HttpResponse(
                    response.status,
                    {key.lower(): value for key, value in response.headers.items()},
                    self._decode(raw),
                )
        except HTTPError as exc:
            raw = exc.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise IntegrationError(
                    IntegrationErrorCode.PAYLOAD_TOO_LARGE,
                    "provider response exceeded the allowed size",
                ) from exc
            return HttpResponse(
                exc.code,
                {key.lower(): value for key, value in exc.headers.items()},
                self._decode(raw),
            )
        except (TimeoutError, socket.timeout) as exc:
            raise IntegrationError(
                IntegrationErrorCode.TIMEOUT,
                "provider request timed out",
                retryable=True,
            ) from exc
        except URLError as exc:
            raise IntegrationError(
                IntegrationErrorCode.PROVIDER_UNAVAILABLE,
                "provider is unavailable",
                retryable=True,
            ) from exc

    @staticmethod
    def _decode(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrationError(
                IntegrationErrorCode.INVALID_PROVIDER_RESPONSE,
                "provider returned an invalid response",
            ) from exc


def validate_https_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("provider base URL must be credential-free HTTPS")
    return value.rstrip("/")
