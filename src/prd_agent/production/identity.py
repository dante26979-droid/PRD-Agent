from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import re
from typing import Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class PrincipalType(StrEnum):
    USER = "USER"
    SERVICE = "SERVICE"


class AuthenticationError(Exception):
    """Stable authentication failure without provider details."""


class AuthorizationError(Exception):
    """Authenticated principal does not have the required scope."""


class UserPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_type: PrincipalType = PrincipalType.USER
    tenant_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    external_subject_hash: str
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    session_id: str | None = Field(default=None, max_length=200)

    def model_post_init(self, _context) -> None:
        if not _DIGEST.fullmatch(self.external_subject_hash):
            raise ValueError("external_subject_hash must be a sha256 digest")

    def require(self, *required_scopes: str) -> None:
        if not set(required_scopes).issubset(self.scopes):
            raise AuthorizationError("principal does not have the required scope")


class PrincipalResolver(Protocol):
    def resolve(self, authorization: str | None) -> UserPrincipal: ...


@dataclass(frozen=True)
class InternalUser:
    tenant_id: str
    user_id: str
    status: str
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()


class IdentityDirectory(Protocol):
    def resolve(self, issuer: str, subject: str) -> InternalUser | None: ...


class StaticIdentityDirectory:
    """Small deterministic directory for CI and local OIDC environments."""

    def __init__(
        self,
        identities: Mapping[tuple[str, str], InternalUser],
    ) -> None:
        self._identities = dict(identities)

    def resolve(self, issuer: str, subject: str) -> InternalUser | None:
        return self._identities.get((issuer, subject))


class PostgresIdentityDirectory:
    def __init__(self, pool) -> None:
        self.pool = pool

    def resolve(self, issuer: str, subject: str) -> InternalUser | None:
        issuer_hash = hashlib.sha256(issuer.encode("utf-8")).hexdigest()
        subject_hash = hashlib.sha256(subject.encode("utf-8")).hexdigest()
        with self.pool.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT identity.tenant_id, identity.user_id, users.status
                      FROM user_identities AS identity
                      JOIN users ON users.user_id = identity.user_id
                                AND users.tenant_id = identity.tenant_id
                     WHERE identity.issuer_hash = %s
                       AND identity.subject_hash = %s
                    """,
                    (issuer_hash, subject_hash),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return InternalUser(
            tenant_id=row[0],
            user_id=row[1],
            status=row[2],
            roles=frozenset({"USER"}),
            scopes=frozenset(
                {
                    "tasks:read",
                    "tasks:write",
                    "evidence:read",
                    "repositories:read",
                    "exports:write",
                    "connections:manage",
                }
            ),
        )


class OidcJwtPrincipalResolver:
    """Validate an OIDC access token and map it to an internal owner.

    Authorization identity never comes directly from mutable profile claims:
    issuer and subject are resolved through the internal identity directory.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwk_client,
        identity_directory: IdentityDirectory,
        allowed_algorithms: tuple[str, ...] = ("RS256",),
        leeway_seconds: int = 30,
    ) -> None:
        if not issuer.startswith("https://"):
            raise ValueError("OIDC issuer must use https")
        if not audience:
            raise ValueError("OIDC audience is required")
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.jwk_client = jwk_client
        self.identity_directory = identity_directory
        self.allowed_algorithms = allowed_algorithms
        self.leeway_seconds = leeway_seconds

    def resolve(self, authorization: str | None) -> UserPrincipal:
        scheme, separator, token = (authorization or "").partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token:
            raise AuthenticationError("authentication is required")
        try:
            import jwt

            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            if algorithm not in self.allowed_algorithms or not header.get("kid"):
                raise AuthenticationError("invalid access token")
            signing_key = self.jwk_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=list(self.allowed_algorithms),
                issuer=self.issuer,
                audience=self.audience,
                leeway=self.leeway_seconds,
                options={
                    "require": ["exp", "iat", "nbf", "iss", "aud", "sub"],
                },
            )
            subject = claims["sub"]
            if not isinstance(subject, str) or not subject:
                raise AuthenticationError("invalid access token")
            user = self.identity_directory.resolve(self.issuer, subject)
            if user is None or user.status != "ACTIVE":
                raise AuthenticationError("access denied")
            claimed_scopes = self._string_set(claims.get("scope", ""))
            claimed_roles = self._string_set(claims.get("roles", ()))
            return UserPrincipal(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                external_subject_hash=(
                    "sha256:"
                    + hashlib.sha256(subject.encode("utf-8")).hexdigest()
                ),
                roles=user.roles.intersection(claimed_roles),
                scopes=user.scopes.intersection(claimed_scopes),
                session_id=None,
            )
        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError("invalid access token") from exc

    @staticmethod
    def _string_set(value) -> frozenset[str]:
        if isinstance(value, str):
            return frozenset(item for item in value.split() if item)
        if isinstance(value, (list, tuple, set, frozenset)) and all(
            isinstance(item, str) for item in value
        ):
            return frozenset(value)
        raise AuthenticationError("invalid access token")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(issuer={self.issuer!r}, "
            f"audience={self.audience!r})"
        )


class LocalPrincipalResolver:
    def __init__(self, user_id: str = "local-user") -> None:
        self.principal = UserPrincipal(
            tenant_id="local",
            user_id=user_id,
            external_subject_hash="sha256:" + "0" * 64,
            roles=frozenset({"LOCAL_USER"}),
            scopes=frozenset(
                {
                    "tasks:read",
                    "tasks:write",
                    "evidence:read",
                    "repositories:read",
                    "exports:write",
                    "connections:manage",
                }
            ),
        )

    def resolve(self, authorization: str | None) -> UserPrincipal:
        return self.principal


class StaticBearerPrincipalResolver:
    """Deterministic resolver for CI and local trusted reverse-proxy tests."""

    def __init__(self, principals_by_token: Mapping[str, UserPrincipal]) -> None:
        self._principals_by_token = dict(principals_by_token)

    def resolve(self, authorization: str | None) -> UserPrincipal:
        scheme, separator, token = (authorization or "").partition(" ")
        if (
            separator != " "
            or scheme.lower() != "bearer"
            or not token
            or token not in self._principals_by_token
        ):
            raise AuthenticationError("authentication is required")
        return self._principals_by_token[token]

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}"
            f"(principals={len(self._principals_by_token)})"
        )
