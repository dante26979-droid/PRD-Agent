from __future__ import annotations

from threading import Lock
import time

from .errors import IntegrationError, IntegrationErrorCode
from .http import JsonHttpTransport, validate_https_base_url


class FeishuTenantAccessTokenResolver:
    """Exchange app credentials for a cached Feishu tenant access token."""

    def __init__(
        self,
        transport: JsonHttpTransport,
        *,
        app_id: str,
        app_secret: str,
        base_url: str = "https://open.feishu.cn/open-apis",
        timeout_seconds: float = 10,
        refresh_margin_seconds: int = 300,
        now=None,
    ) -> None:
        if not app_id or not app_secret:
            raise ValueError("Feishu app_id and app_secret are required")
        self.transport = transport
        self._app_id = app_id
        self._app_secret = app_secret
        self.base_url = validate_https_base_url(base_url)
        self.timeout_seconds = timeout_seconds
        self.refresh_margin_seconds = refresh_margin_seconds
        self.now = now or time.monotonic
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = Lock()

    def resolve(self, connection_id: str) -> str:
        del connection_id
        now = self.now()
        if self._token and now < self._expires_at:
            return self._token
        with self._lock:
            now = self.now()
            if self._token and now < self._expires_at:
                return self._token
            response = self.transport.request(
                "POST",
                self.base_url + "/auth/v3/tenant_access_token/internal",
                headers={"Content-Type": "application/json; charset=utf-8"},
                json_body={
                    "app_id": self._app_id,
                    "app_secret": self._app_secret,
                },
                timeout_seconds=self.timeout_seconds,
            )
            if not isinstance(response.body, dict) or response.body.get("code") != 0:
                raise IntegrationError(
                    IntegrationErrorCode.UNAUTHORIZED,
                    "Feishu application credentials were rejected",
                )
            token = response.body.get("tenant_access_token")
            expires_in = response.body.get("expire")
            if (
                not isinstance(token, str)
                or not token
                or not isinstance(expires_in, int)
                or expires_in <= 0
            ):
                raise IntegrationError(
                    IntegrationErrorCode.INVALID_PROVIDER_RESPONSE,
                    "Feishu returned an invalid access token response",
                )
            self._token = token
            usable_seconds = max(1, expires_in - self.refresh_margin_seconds)
            self._expires_at = now + usable_seconds
            return token

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"app_id={self._app_id!r}, cached={self._token is not None})"
        )
