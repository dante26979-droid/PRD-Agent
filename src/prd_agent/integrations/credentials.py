from __future__ import annotations

from typing import Mapping, Protocol

from .errors import IntegrationError, IntegrationErrorCode


class CredentialResolver(Protocol):
    def resolve(self, connection_id: str) -> str: ...


class StaticCredentialResolver:
    """Portfolio adapter for server-configured credentials.

    Values are deliberately inaccessible through repr/serialization.
    """

    def __init__(self, credentials: Mapping[str, str]) -> None:
        self._credentials = dict(credentials)

    def resolve(self, connection_id: str) -> str:
        credential = self._credentials.get(connection_id)
        if not credential:
            raise IntegrationError(
                IntegrationErrorCode.UNAUTHORIZED,
                "provider authorization is not available",
            )
        return credential

    def __repr__(self) -> str:
        return f"{type(self).__name__}(connections={len(self._credentials)})"
