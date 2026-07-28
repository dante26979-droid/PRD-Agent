from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from prd_agent.production.identity import (
    AuthenticationError,
    InternalUser,
    OidcJwtPrincipalResolver,
    StaticIdentityDirectory,
)


@dataclass(frozen=True)
class SigningKey:
    key: object


class FixedJwkClient:
    def __init__(self, public_key) -> None:
        self.public_key = public_key
        self.calls = 0

    def get_signing_key_from_jwt(self, token: str) -> SigningKey:
        self.calls += 1
        return SigningKey(self.public_key)


def _token(private_key, **overrides) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "iss": "https://identity.example.test",
        "sub": "provider-subject-alice",
        "aud": "prd-agent-api",
        "iat": now,
        "nbf": now - timedelta(seconds=1),
        "exp": now + timedelta(minutes=5),
        "scope": "tasks:read tasks:write",
        "roles": ["USER"],
        **overrides,
    }
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key-1"},
    )


def _resolver():
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    directory = StaticIdentityDirectory(
        {
            (
                "https://identity.example.test",
                "provider-subject-alice",
            ): InternalUser(
                tenant_id="tenant-a",
                user_id="alice",
                status="ACTIVE",
                roles=frozenset({"USER"}),
                scopes=frozenset({"tasks:read", "tasks:write"}),
            )
        }
    )
    return (
        private_key,
        OidcJwtPrincipalResolver(
            issuer="https://identity.example.test",
            audience="prd-agent-api",
            jwk_client=FixedJwkClient(private_key.public_key()),
            identity_directory=directory,
        ),
    )


def test_oidc_resolver_verifies_signature_and_maps_subject_to_internal_owner():
    private_key, resolver = _resolver()

    principal = resolver.resolve(f"Bearer {_token(private_key)}")

    assert principal.tenant_id == "tenant-a"
    assert principal.user_id == "alice"
    assert principal.scopes == frozenset({"tasks:read", "tasks:write"})
    assert "provider-subject-alice" not in repr(principal)


@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"iss": "https://attacker.example.test"},
        {"aud": "some-other-api"},
        {"exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
    ],
)
def test_oidc_resolver_rejects_untrusted_or_expired_tokens(claim_overrides):
    private_key, resolver = _resolver()

    with pytest.raises(AuthenticationError, match="invalid access token"):
        resolver.resolve(
            f"Bearer {_token(private_key, **claim_overrides)}"
        )
