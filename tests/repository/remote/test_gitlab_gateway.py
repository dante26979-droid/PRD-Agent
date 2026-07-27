from __future__ import annotations

import base64

from prd_agent.integrations.credentials import StaticCredentialResolver
from prd_agent.integrations.http import HttpResponse
from prd_agent.repository.remote.gitlab import GitLabRepositoryGateway
from prd_agent.repository.remote.models import RemoteRepositoryBinding


COMMIT = "b" * 40


class FakeTransport:
    def __init__(self) -> None:
        self.requests = []

    def request(self, method, url, *, headers=None, timeout_seconds=10):
        self.requests.append((method, url, headers))
        if "/commits/" in url:
            return HttpResponse(200, {}, {"id": COMMIT})
        if "/repository/tree" in url:
            return HttpResponse(
                200,
                {"x-next-page": ""},
                [
                    {
                        "path": "src/app.py",
                        "type": "blob",
                        "mode": "100644",
                        "id": "blob-1",
                    }
                ],
            )
        return HttpResponse(
            200,
            {},
            {
                "blob_id": "blob-1",
                "encoding": "base64",
                "content": base64.b64encode(b"print('gitlab')\n").decode(),
                "size": 16,
            },
        )


def binding():
    return RemoteRepositoryBinding(
        repository_id="remote-orders",
        owner_id="local-user",
        connection_id="gitlab-connection",
        provider="GITLAB",
        provider_repository_id="acme/orders",
        display_name="acme/orders",
        default_revision="main",
        access_scope_hash="sha256:" + "2" * 64,
    )


def test_gitlab_gateway_reads_commit_tree_and_blob_with_server_side_credential():
    transport = FakeTransport()
    gateway = GitLabRepositoryGateway(
        transport,
        StaticCredentialResolver({"gitlab-connection": "secret-token"}),
    )

    assert gateway.resolve_commit(binding(), "main") == COMMIT
    assert gateway.list_blobs(binding(), COMMIT)[0].path == "src/app.py"
    assert gateway.read_blob(binding(), COMMIT, "src/app.py").data == b"print('gitlab')\n"

    assert all("secret-token" not in url for _, url, _ in transport.requests)
    assert all(headers["PRIVATE-TOKEN"] == "secret-token" for _, _, headers in transport.requests)
