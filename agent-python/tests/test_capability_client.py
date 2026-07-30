from __future__ import annotations

from agent.capability import CapabilityGatewayClient, CapabilitySession
from agent.context import Lease
from agent.v1 import capability_gateway_pb2 as capability


class FakeCapabilityStub:
    def __init__(self):
        self.requests = []

    def SearchRepository(self, request, timeout=None):
        self.requests.append((request, timeout))
        return capability.SearchRepositoryResponse(
            hits=[
                capability.RepositorySearchHit(
                    path="backend-go/internal/dispatcher/dispatcher.go",
                    line=42,
                    snippet="type Dispatcher struct",
                )
            ]
        )

    def ReadRepositoryFile(self, request, timeout=None):
        self.requests.append((request, timeout))
        return capability.ReadRepositoryFileResponse(
            file=capability.RepositoryFile(
                path=request.path,
                content=b"x" * 32,
                content_hash="sha256:test",
            )
        )

    def SearchPrdCatalog(self, request, timeout=None):
        self.requests.append((request, timeout))
        return capability.SearchPrdCatalogResponse(
            hits=[
                capability.PrdCatalogHit(
                    locator_id="locator-1",
                    source_revision="revision-1",
                    title="历史 PRD",
                    excerpt="与当前需求相关",
                )
            ]
        )

    def FetchPrdSections(self, request, timeout=None):
        self.requests.append((request, timeout))
        return capability.FetchPrdSectionsResponse(
            sections=[
                capability.PrdSection(
                    locator_id="locator-1",
                    source_revision="revision-1",
                    title="约束",
                    markdown="只能通过 Go Gateway 获取内容",
                )
            ]
        )


class MetadataCapabilityStub(FakeCapabilityStub):
    def SearchRepository(self, request, timeout=None, metadata=None):
        self.metadata = metadata
        return super().SearchRepository(request, timeout=timeout)


def test_capability_client_binds_repository_search_to_current_lease():
    stub = FakeCapabilityStub()
    client = CapabilityGatewayClient(
        stub,
        CapabilitySession(
            lease=Lease(
                run_id="run-1",
                lease_id="lease-1",
                worker_id="worker-1",
                fencing_token=3,
                expires_at="2030-01-01T00:00:00Z",
            ),
            request_id_prefix="dispatch-1",
            correlation_id="run-1",
        ),
        timeout_seconds=4,
    )

    hits = client.search_repository(
        binding_id="binding-1",
        revision="a" * 40,
        query="dispatcher",
        limit=10,
    )

    request, timeout = stub.requests[0]
    assert hits[0].path == "backend-go/internal/dispatcher/dispatcher.go"
    assert request.capability.lease.fencing_token == 3
    assert request.capability.meta.request_id == "dispatch-1:search-repository:1"
    assert request.capability.meta.correlation_id == "run-1"
    assert timeout == 4


def test_capability_client_rejects_oversized_repository_content():
    client = CapabilityGatewayClient(
        FakeCapabilityStub(),
        CapabilitySession(
            lease=Lease("run-1", "lease-1", "worker-1", 1, "2030-01-01T00:00:00Z"),
            request_id_prefix="dispatch-1",
            correlation_id="run-1",
        ),
        max_content_bytes=16,
    )

    try:
        client.read_repository_file(
            binding_id="binding-1",
            revision="a" * 40,
            path="README.md",
        )
    except Exception as error:
        assert getattr(error, "code", "") == "CONTENT_LIMIT_EXCEEDED"
        assert getattr(error, "retryable", True) is False
    else:
        raise AssertionError("oversized capability content must be rejected")


def test_capability_client_sends_internal_service_identity():
    stub = MetadataCapabilityStub()
    client = CapabilityGatewayClient(
        stub,
        CapabilitySession(
            lease=Lease("run-1", "lease-1", "worker-1", 1, "2030-01-01T00:00:00Z"),
            request_id_prefix="dispatch-1",
            correlation_id="run-1",
        ),
        service_token="s" * 32,
    )

    client.search_repository(
        binding_id="binding-1",
        revision="a" * 40,
        query="dispatcher",
    )

    assert stub.metadata == (("authorization", "Bearer " + ("s" * 32)),)


def test_capability_client_fetches_catalog_candidates_then_source_sections():
    client = CapabilityGatewayClient(
        FakeCapabilityStub(),
        CapabilitySession(
            lease=Lease("run-1", "lease-1", "worker-1", 1, "2030-01-01T00:00:00Z"),
            request_id_prefix="dispatch-1",
            correlation_id="run-1",
        ),
    )

    hits = client.search_prd_catalog(query="权限边界", limit=5)
    sections = client.fetch_prd_sections([hits[0].locator_id])

    assert hits[0].title == "历史 PRD"
    assert sections[0].source_revision == "revision-1"
    assert "Go Gateway" in sections[0].markdown
