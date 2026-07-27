from __future__ import annotations

import pytest

from prd_agent.integrations.errors import IntegrationError, IntegrationErrorCode
from prd_agent.repository.models import BlobContent, BlobEntry
from prd_agent.repository.remote.models import RemoteRepositoryBinding
from prd_agent.repository.remote.object_reader import (
    RemoteRepositoryCatalog,
    RemoteRepositoryObjectReader,
)
from prd_agent.tools.default_registry import build_repository_tool_registry
from prd_agent.tools.models import ToolAction


COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


class FakeRemoteGateway:
    def __init__(self) -> None:
        self.refs = {"main": COMMIT_A}
        self.blobs = {
            (COMMIT_A, "src/rules.py"): b"STATUS = 'paid'\n",
            (COMMIT_B, "src/rules.py"): b"STATUS = 'completed'\n",
        }

    def resolve_commit(self, binding, revision):
        return self.refs.get(revision, revision)

    def list_blobs(self, binding, commit_sha):
        return tuple(
            BlobEntry(path=path, blob_id=f"blob-{commit_sha[0]}", size=len(data), mode="100644")
            for (sha, path), data in self.blobs.items()
            if sha == commit_sha
        )

    def read_blob(self, binding, commit_sha, path):
        data = self.blobs[(commit_sha, path)]
        return BlobContent(path=path, blob_id=f"blob-{commit_sha[0]}", data=data)


def test_remote_snapshot_keeps_reading_the_resolved_commit_after_branch_moves():
    gateway = FakeRemoteGateway()
    reader = RemoteRepositoryObjectReader(
        RemoteRepositoryCatalog(
            [
                RemoteRepositoryBinding(
                    repository_id="remote-orders",
                    owner_id="local-user",
                    connection_id="connection-1",
                    provider="GITHUB",
                    provider_repository_id="acme/orders",
                    display_name="acme/orders",
                    default_revision="main",
                    allowed_prefix="src",
                    access_scope_hash="sha256:" + "1" * 64,
                )
            ]
        ),
        {"GITHUB": gateway},
    )

    snapshot = reader.resolve_snapshot("remote-orders")
    gateway.refs["main"] = COMMIT_B

    blob = reader.read_blob(snapshot, "rules.py")

    assert snapshot.resolved_commit_sha == COMMIT_A
    assert snapshot.resolver_version == "remote-github.v1"
    assert blob.path == "rules.py"
    assert blob.data == b"STATUS = 'paid'\n"
    assert [item.path for item in reader.list_blobs(snapshot)] == ["rules.py"]


def test_remote_reader_runs_through_the_existing_repository_tool_contract():
    gateway = FakeRemoteGateway()
    reader = RemoteRepositoryObjectReader(
        RemoteRepositoryCatalog(
            [
                RemoteRepositoryBinding(
                    repository_id="remote-orders",
                    owner_id="local-user",
                    connection_id="connection-1",
                    provider="GITHUB",
                    provider_repository_id="acme/orders",
                    display_name="acme/orders",
                    default_revision=COMMIT_A,
                    allowed_prefix="src",
                    access_scope_hash="sha256:" + "1" * 64,
                )
            ]
        ),
        {"GITHUB": gateway},
    )
    snapshot = reader.resolve_snapshot("remote-orders")
    action = ToolAction(
        tool_id="search_text",
        tool_schema_version="1",
        repository_id="remote-orders",
        resolved_commit_sha=COMMIT_A,
        arguments={"query": "paid"},
        purpose="定位状态规则",
    )
    validated = build_repository_tool_registry(reader).validate(action)

    result = validated.handler.execute(snapshot, validated.arguments)

    assert result.status == "SUCCEEDED"
    assert result.items[0].path == "rules.py"
    assert "paid" in result.items[0].excerpt


def test_remote_reader_does_not_turn_provider_auth_failure_into_not_found():
    class UnauthorizedGateway(FakeRemoteGateway):
        def resolve_commit(self, binding, revision):
            raise IntegrationError(
                IntegrationErrorCode.UNAUTHORIZED,
                "provider authorization is not available",
            )

    reader = RemoteRepositoryObjectReader(
        RemoteRepositoryCatalog(
            [
                RemoteRepositoryBinding(
                    repository_id="remote-orders",
                    owner_id="local-user",
                    connection_id="connection-1",
                    provider="GITHUB",
                    provider_repository_id="acme/orders",
                    display_name="acme/orders",
                    default_revision="main",
                    access_scope_hash="sha256:" + "1" * 64,
                )
            ]
        ),
        {"GITHUB": UnauthorizedGateway()},
    )

    with pytest.raises(IntegrationError) as captured:
        reader.resolve_snapshot("remote-orders")

    assert captured.value.code == IntegrationErrorCode.UNAUTHORIZED
