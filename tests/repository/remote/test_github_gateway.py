from __future__ import annotations

import base64
import pytest

from prd_agent.integrations.http import HttpResponse
from prd_agent.integrations.credentials import StaticCredentialResolver
from prd_agent.integrations.errors import IntegrationError, IntegrationErrorCode
from prd_agent.repository.remote.github import GitHubRepositoryGateway
from prd_agent.repository.remote.models import RemoteRepositoryBinding


COMMIT = "a" * 40


class FakeTransport:
    def __init__(self) -> None:
        self.requests = []

    def request(self, method, url, *, headers=None, timeout_seconds=10):
        self.requests.append((method, url, headers))
        if "/commits/" in url:
            return HttpResponse(200, {}, {"sha": COMMIT})
        if "/git/trees/" in url:
            return HttpResponse(
                200,
                {},
                {
                    "truncated": False,
                    "tree": [
                        {
                            "path": "src/app.py",
                            "type": "blob",
                            "mode": "100644",
                            "sha": "blob-1",
                            "size": 12,
                        }
                    ],
                },
            )
        return HttpResponse(
            200,
            {},
            {
                "type": "file",
                "sha": "blob-1",
                "encoding": "base64",
                "content": base64.b64encode(b"print('ok')\n").decode(),
            },
        )


def binding():
    return RemoteRepositoryBinding(
        repository_id="remote-orders",
        owner_id="local-user",
        connection_id="github-connection",
        provider="GITHUB",
        provider_repository_id="acme/orders",
        display_name="acme/orders",
        default_revision="main",
        access_scope_hash="sha256:" + "1" * 64,
    )


def test_github_gateway_reads_commit_tree_and_blob_with_server_side_credential():
    transport = FakeTransport()
    gateway = GitHubRepositoryGateway(
        transport,
        StaticCredentialResolver({"github-connection": "secret-token"}),
    )

    assert gateway.resolve_commit(binding(), "main") == COMMIT
    assert gateway.list_blobs(binding(), COMMIT)[0].path == "src/app.py"
    assert gateway.read_blob(binding(), COMMIT, "src/app.py").data == b"print('ok')\n"

    assert all("secret-token" not in url for _, url, _ in transport.requests)
    assert all(headers["Authorization"] == "Bearer secret-token" for _, _, headers in transport.requests)


def test_remote_gateway_rejects_insecure_or_credential_bearing_base_urls():
    for value in (
        "http://api.github.test",
        "https://user:secret@api.github.test",
        "https://api.github.test?token=secret",
    ):
        with pytest.raises(ValueError):
            GitHubRepositoryGateway(
                FakeTransport(),
                StaticCredentialResolver({"github-connection": "secret-token"}),
                base_url=value,
            )


def test_github_rate_limit_is_retryable_with_bounded_retry_after():
    class RateLimitedTransport:
        def request(self, method, url, *, headers=None, timeout_seconds=10):
            return HttpResponse(429, {"retry-after": "999999"}, {})

    gateway = GitHubRepositoryGateway(
        RateLimitedTransport(),
        StaticCredentialResolver({"github-connection": "secret-token"}),
    )

    with pytest.raises(IntegrationError) as captured:
        gateway.resolve_commit(binding(), "main")

    assert captured.value.code == IntegrationErrorCode.RATE_LIMITED
    assert captured.value.retryable is True
    assert captured.value.retry_after_seconds == 300
