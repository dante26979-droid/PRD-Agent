from __future__ import annotations

import io
from urllib.error import HTTPError

import pytest

from prd_agent.integrations.credentials import StaticCredentialResolver
from prd_agent.integrations.errors import IntegrationError, IntegrationErrorCode
from prd_agent.integrations.http import (
    UrllibJsonHttpTransport,
    validate_https_base_url,
)


def test_static_credentials_are_resolved_only_by_connection_and_hidden_from_repr():
    marker = "credential-marker-that-must-not-leak"
    resolver = StaticCredentialResolver({"connection-1": marker})

    assert resolver.resolve("connection-1") == marker
    assert marker not in repr(resolver)

    with pytest.raises(IntegrationError) as captured:
        resolver.resolve("unknown-connection")

    assert captured.value.code == IntegrationErrorCode.UNAUTHORIZED
    assert marker not in str(captured.value)


@pytest.mark.parametrize(
    "value",
    [
        "http://provider.example/api",
        "https://user:secret@provider.example/api",
        "https://provider.example/api?token=secret",
        "https://provider.example/api#secret",
    ],
)
def test_provider_base_url_rejects_insecure_or_credential_bearing_values(value):
    with pytest.raises(ValueError):
        validate_https_base_url(value)


def test_http_transport_rejects_oversized_error_body_without_exposing_it(
    monkeypatch,
):
    marker = b"provider-private-error-marker"

    def raise_http_error(*_args, **_kwargs):
        raise HTTPError(
            "https://provider.example/api",
            500,
            "private provider reason",
            {},
            io.BytesIO(marker),
        )

    monkeypatch.setattr(
        "prd_agent.integrations.http.urlopen",
        raise_http_error,
    )
    transport = UrllibJsonHttpTransport(max_response_bytes=8)

    with pytest.raises(IntegrationError) as captured:
        transport.request("GET", "https://provider.example/api")

    assert captured.value.code == IntegrationErrorCode.PAYLOAD_TOO_LARGE
    assert marker.decode() not in str(captured.value)
