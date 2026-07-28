from __future__ import annotations

from typing import Protocol


class ExternalIdProtector(Protocol):
    """Encryption boundary supplied by the deployment's secret manager/KMS adapter."""

    def protect(self, value: str) -> str: ...

    def unprotect(self, value: str) -> str: ...
