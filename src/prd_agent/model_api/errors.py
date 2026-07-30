from __future__ import annotations


class ModelApiError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(f"model API request failed: {code}")
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
