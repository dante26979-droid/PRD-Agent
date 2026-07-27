from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac

import pytest

from prd_agent.production.integrations import (
    InMemoryConnectionStore,
    InMemorySecretStore,
    OAuthCoordinator,
    OAuthSecurityError,
    WebhookVerifier,
)


class RecordingProvider:
    def __init__(self) -> None:
        self.calls = []

    def exchange_code(self, *, code: str, pkce_verifier: str) -> dict:
        self.calls.append((code, pkce_verifier))
        return {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "expires_in": 3600,
        }


def test_oauth_pkce_state_is_owner_scoped_and_tokens_only_enter_secret_store():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    secrets = InMemorySecretStore()
    store = InMemoryConnectionStore()
    provider = RecordingProvider()
    coordinator = OAuthCoordinator(
        store=store,
        secret_store=secrets,
        providers={"GITHUB": provider},
    )

    authorization = coordinator.begin(
        provider="GITHUB",
        tenant_id="tenant-a",
        owner_id="alice",
        now=now,
    )

    assert authorization.code_challenge
    assert authorization.state
    assert "verifier" not in repr(authorization).lower()
    with pytest.raises(OAuthSecurityError):
        coordinator.complete(
            provider="GITHUB",
            tenant_id="tenant-a",
            owner_id="bob",
            state=authorization.state,
            code="stolen-code",
            now=now + timedelta(seconds=1),
        )
    assert provider.calls == []

    connection = coordinator.complete(
        provider="GITHUB",
        tenant_id="tenant-a",
        owner_id="alice",
        state=authorization.state,
        code="valid-code",
        now=now + timedelta(seconds=1),
    )

    assert connection.owner_id == "alice"
    assert connection.credential_ref.startswith("secret://")
    assert "access-secret" not in repr(connection)
    assert "access-secret" in secrets.get(connection.credential_ref)
    with pytest.raises(OAuthSecurityError):
        coordinator.complete(
            provider="GITHUB",
            tenant_id="tenant-a",
            owner_id="alice",
            state=authorization.state,
            code="replay",
            now=now + timedelta(seconds=2),
        )


def test_webhook_signature_window_and_delivery_id_are_enforced():
    verifier = WebhookVerifier(
        signing_secrets={"GITHUB": b"webhook-secret"},
        tolerance=timedelta(minutes=5),
    )
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    payload = b'{"repository":{"id":123}}'
    timestamp = int(now.timestamp())
    signature = hmac.new(
        b"webhook-secret",
        f"{timestamp}.".encode() + payload,
        hashlib.sha256,
    ).hexdigest()

    assert verifier.verify(
        provider="GITHUB",
        delivery_id="delivery-1",
        timestamp=timestamp,
        payload=payload,
        signature=signature,
        now=now,
    )
    assert not verifier.verify(
        provider="GITHUB",
        delivery_id="delivery-1",
        timestamp=timestamp,
        payload=payload,
        signature=signature,
        now=now,
    )
    with pytest.raises(OAuthSecurityError):
        verifier.verify(
            provider="GITHUB",
            delivery_id="delivery-2",
            timestamp=timestamp - 600,
            payload=payload,
            signature=signature,
            now=now,
        )
