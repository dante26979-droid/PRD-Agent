from __future__ import annotations

import base64
import hashlib
import os
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class ExternalIdProtector(Protocol):
    """Encryption boundary supplied by the deployment's secret manager/KMS adapter."""

    def protect(self, value: str) -> str: ...

    def unprotect(self, value: str) -> str: ...


class AesGcmExternalIdProtector:
    """Encrypt provider identifiers before storing them in PostgreSQL."""

    _ASSOCIATED_DATA = b"prd-agent:feishu-external-id:v1"

    def __init__(self, secret: str) -> None:
        if len(secret.encode("utf-8")) < 16:
            raise ValueError("external ID encryption key must be at least 16 bytes")
        self._key = hashlib.sha256(secret.encode("utf-8")).digest()

    def protect(self, value: str) -> str:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(
            nonce,
            value.encode("utf-8"),
            self._ASSOCIATED_DATA,
        )
        encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"v1:{encoded}"

    def unprotect(self, value: str) -> str:
        if not value.startswith("v1:"):
            raise ValueError("unsupported external ID ciphertext")
        payload = base64.urlsafe_b64decode(value.removeprefix("v1:"))
        if len(payload) <= 12:
            raise ValueError("invalid external ID ciphertext")
        plaintext = AESGCM(self._key).decrypt(
            payload[:12],
            payload[12:],
            self._ASSOCIATED_DATA,
        )
        return plaintext.decode("utf-8")
