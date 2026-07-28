from __future__ import annotations


class DraftQualityError(ValueError):
    pass


class DraftQualityPolicy:
    """Deterministic safety and shape gate before Draft submission."""

    _sensitive_markers = (
        "authorization:",
        "bearer ",
        "api_key",
        "access_token",
        "refresh_token",
        "private_key",
    )

    def __init__(self, *, max_bytes: int = 1024 * 1024) -> None:
        if max_bytes < 1:
            raise ValueError("draft quality size limit must be positive")
        self._max_bytes = max_bytes

    def validate(self, markdown: str) -> str:
        value = markdown.strip()
        if not value:
            raise DraftQualityError("draft markdown is empty")
        if len(value.encode("utf-8")) > self._max_bytes:
            raise DraftQualityError("draft markdown exceeds size limit")
        if not value.startswith("#"):
            raise DraftQualityError("draft markdown requires a heading")
        lowered = value.lower()
        if any(marker in lowered for marker in self._sensitive_markers):
            raise DraftQualityError("draft contains a sensitive marker")
        return value
