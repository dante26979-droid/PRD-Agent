from __future__ import annotations

from enum import StrEnum


class IntegrationErrorCode(StrEnum):
    UNAUTHORIZED = "UNAUTHORIZED"
    RESOURCE_NOT_ALLOWED = "RESOURCE_NOT_ALLOWED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    VERSION_NOT_FOUND = "VERSION_NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INVALID_PROVIDER_RESPONSE = "INVALID_PROVIDER_RESPONSE"
    RESOURCE_CHANGED = "RESOURCE_CHANGED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_STATE = "INVALID_STATE"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"


class IntegrationError(Exception):
    def __init__(
        self,
        code: IntegrationErrorCode,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
