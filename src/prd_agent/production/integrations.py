"""OAuth, secret, and webhook security primitives for production adapters."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import secrets
from threading import RLock
from typing import Mapping, Protocol
import uuid


class OAuthSecurityError(RuntimeError):
    pass


class SecretStore(Protocol):
    def put(self, value: str) -> str: ...

    def get(self, reference: str) -> str: ...

    def delete(self, reference: str) -> None: ...


class InMemorySecretStore:
    """Test adapter with the same opaque-reference boundary as a secret manager."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._lock = RLock()

    def put(self, value: str) -> str:
        reference = f"secret://{uuid.uuid4().hex}"
        with self._lock:
            self._values[reference] = value
        return reference

    def get(self, reference: str) -> str:
        with self._lock:
            return self._values[reference]

    def delete(self, reference: str) -> None:
        with self._lock:
            self._values.pop(reference, None)


@dataclass(frozen=True)
class OAuthAuthorization:
    provider: str
    state: str
    code_challenge: str
    code_challenge_method: str = "S256"


@dataclass(frozen=True)
class OAuthAuthorizationTransaction:
    transaction_id: str
    provider: str
    tenant_id: str
    owner_id: str
    state_hash: str
    pkce_verifier_ref: str = field(repr=False)
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass(frozen=True)
class ExternalConnection:
    connection_id: str
    provider: str
    tenant_id: str
    owner_id: str
    credential_ref: str
    status: str = "ACTIVE"


class OAuthProvider(Protocol):
    def exchange_code(self, *, code: str, pkce_verifier: str) -> dict: ...


class InMemoryConnectionStore:
    def __init__(self) -> None:
        self._transactions: dict[str, OAuthAuthorizationTransaction] = {}
        self._connections: dict[str, ExternalConnection] = {}
        self._lock = RLock()

    def save_transaction(self, value: OAuthAuthorizationTransaction) -> None:
        with self._lock:
            if value.state_hash in self._transactions:
                raise OAuthSecurityError("authorization state collision")
            self._transactions[value.state_hash] = value

    def consume_transaction(
        self,
        *,
        state_hash: str,
        provider: str,
        tenant_id: str,
        owner_id: str,
        now: datetime,
    ) -> OAuthAuthorizationTransaction:
        with self._lock:
            value = self._transactions.get(state_hash)
            if (
                value is None
                or value.provider != provider
                or value.tenant_id != tenant_id
                or value.owner_id != owner_id
                or value.consumed_at is not None
                or value.expires_at <= now
            ):
                raise OAuthSecurityError("invalid authorization transaction")
            consumed = OAuthAuthorizationTransaction(
                **{
                    **value.__dict__,
                    "consumed_at": now,
                }
            )
            self._transactions[state_hash] = consumed
            return consumed

    def save_connection(self, value: ExternalConnection) -> None:
        with self._lock:
            self._connections[value.connection_id] = value

    def get_connection(
        self,
        connection_id: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> ExternalConnection:
        with self._lock:
            value = self._connections.get(connection_id)
            if (
                value is None
                or value.tenant_id != tenant_id
                or value.owner_id != owner_id
            ):
                raise KeyError(connection_id)
            return value


class OAuthCoordinator:
    def __init__(
        self,
        *,
        store: InMemoryConnectionStore,
        secret_store: SecretStore,
        providers: Mapping[str, OAuthProvider],
        transaction_ttl: timedelta = timedelta(minutes=10),
    ) -> None:
        self.store = store
        self.secret_store = secret_store
        self.providers = dict(providers)
        self.transaction_ttl = transaction_ttl

    def begin(
        self,
        *,
        provider: str,
        tenant_id: str,
        owner_id: str,
        now: datetime,
    ) -> OAuthAuthorization:
        if provider not in self.providers:
            raise OAuthSecurityError("unsupported provider")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        verifier_ref = self.secret_store.put(verifier)
        try:
            self.store.save_transaction(
                OAuthAuthorizationTransaction(
                    transaction_id=f"oauth-{uuid.uuid4().hex}",
                    provider=provider,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    state_hash=self._digest(state),
                    pkce_verifier_ref=verifier_ref,
                    expires_at=now + self.transaction_ttl,
                )
            )
        except Exception:
            self.secret_store.delete(verifier_ref)
            raise
        return OAuthAuthorization(
            provider=provider,
            state=state,
            code_challenge=challenge,
        )

    def complete(
        self,
        *,
        provider: str,
        tenant_id: str,
        owner_id: str,
        state: str,
        code: str,
        now: datetime,
    ) -> ExternalConnection:
        transaction = self.store.consume_transaction(
            state_hash=self._digest(state),
            provider=provider,
            tenant_id=tenant_id,
            owner_id=owner_id,
            now=now,
        )
        verifier = self.secret_store.get(transaction.pkce_verifier_ref)
        try:
            token_set = self.providers[provider].exchange_code(
                code=code,
                pkce_verifier=verifier,
            )
        finally:
            self.secret_store.delete(transaction.pkce_verifier_ref)
        if not isinstance(token_set.get("access_token"), str):
            raise OAuthSecurityError("provider returned an invalid token response")
        credential_ref = self.secret_store.put(
            json.dumps(token_set, separators=(",", ":"), sort_keys=True)
        )
        connection = ExternalConnection(
            connection_id=f"connection-{uuid.uuid4().hex}",
            provider=provider,
            tenant_id=tenant_id,
            owner_id=owner_id,
            credential_ref=credential_ref,
        )
        try:
            self.store.save_connection(connection)
        except Exception:
            self.secret_store.delete(credential_ref)
            raise
        return connection

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


class WebhookVerifier:
    def __init__(
        self,
        *,
        signing_secrets: Mapping[str, bytes],
        tolerance: timedelta,
        max_payload_bytes: int = 1_000_000,
    ) -> None:
        self.signing_secrets = dict(signing_secrets)
        self.tolerance = tolerance
        self.max_payload_bytes = max_payload_bytes
        self._deliveries: set[tuple[str, str]] = set()
        self._lock = RLock()

    def verify(
        self,
        *,
        provider: str,
        delivery_id: str,
        timestamp: int,
        payload: bytes,
        signature: str,
        now: datetime,
    ) -> bool:
        secret = self.signing_secrets.get(provider)
        if secret is None or not delivery_id or len(payload) > self.max_payload_bytes:
            raise OAuthSecurityError("invalid webhook")
        if abs(now.timestamp() - timestamp) > self.tolerance.total_seconds():
            raise OAuthSecurityError("invalid webhook")
        expected = hmac.new(
            secret,
            f"{timestamp}.".encode("ascii") + payload,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise OAuthSecurityError("invalid webhook")
        key = (provider, delivery_id)
        with self._lock:
            if key in self._deliveries:
                return False
            self._deliveries.add(key)
            return True
