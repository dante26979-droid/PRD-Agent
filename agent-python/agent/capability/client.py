from __future__ import annotations

from dataclasses import dataclass
from itertools import count

import grpc

from agent.context import Lease
from agent.v1 import agent_execution_pb2 as execution
from agent.v1 import capability_gateway_pb2 as capability
from agent.v1 import capability_gateway_pb2_grpc as capability_rpc


CONTRACT_VERSION = "agent-execution.v1"


class CapabilityError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class CapabilitySession:
    lease: Lease
    request_id_prefix: str
    correlation_id: str


@dataclass(frozen=True)
class RepositorySearchHit:
    path: str
    line: int
    snippet: str


@dataclass(frozen=True)
class RepositoryFile:
    path: str
    content: bytes
    content_hash: str


@dataclass(frozen=True)
class PrdCatalogHit:
    locator_id: str
    source_revision: str
    title: str
    excerpt: str


@dataclass(frozen=True)
class PrdSection:
    locator_id: str
    source_revision: str
    title: str
    markdown: str


class CapabilityGatewayClient:
    """Agent-facing Adapter for Go-owned repository and PRD capabilities."""

    def __init__(
        self,
        stub,
        session: CapabilitySession,
        *,
        timeout_seconds: float = 10.0,
        max_content_bytes: int = 512 * 1024,
    ) -> None:
        if not session.request_id_prefix or not session.correlation_id:
            raise ValueError("capability request and correlation identifiers are required")
        if timeout_seconds <= 0:
            raise ValueError("capability timeout must be positive")
        if max_content_bytes < 1:
            raise ValueError("capability content limit must be positive")
        self._stub = stub
        self._session = session
        self._timeout = timeout_seconds
        self._max_content_bytes = max_content_bytes
        self._counter = count(1)

    @classmethod
    def connect(
        cls,
        target: str,
        session: CapabilitySession,
        *,
        timeout_seconds: float = 10.0,
    ) -> "CapabilityGatewayClient":
        if not target:
            raise ValueError("capability target is required")
        channel = grpc.insecure_channel(target)
        client = cls(
            capability_rpc.CapabilityGatewayServiceStub(channel),
            session,
            timeout_seconds=timeout_seconds,
        )
        client._channel = channel
        return client

    def search_repository(
        self,
        *,
        binding_id: str,
        revision: str,
        query: str,
        limit: int = 20,
    ) -> tuple[RepositorySearchHit, ...]:
        if not binding_id or not revision or not query.strip():
            raise ValueError("binding_id, revision and query are required")
        if not 1 <= limit <= 100:
            raise ValueError("repository search limit must be between 1 and 100")
        request = capability.SearchRepositoryRequest(
            capability=self._capability("search-repository"),
            binding_id=binding_id,
            revision=revision,
            query=query,
            limit=limit,
        )
        response = self._call(self._stub.SearchRepository, request)
        return tuple(
            RepositorySearchHit(path=item.path, line=item.line, snippet=item.snippet)
            for item in response.hits
        )

    def read_repository_file(
        self,
        *,
        binding_id: str,
        revision: str,
        path: str,
    ) -> RepositoryFile:
        if not binding_id or not revision or not path:
            raise ValueError("binding_id, revision and path are required")
        response = self._call(
            self._stub.ReadRepositoryFile,
            capability.ReadRepositoryFileRequest(
                capability=self._capability("read-repository-file"),
                binding_id=binding_id,
                revision=revision,
                path=path,
            ),
        )
        value = response.file
        if len(value.content) > self._max_content_bytes:
            raise CapabilityError(
                "CONTENT_LIMIT_EXCEEDED",
                "repository file exceeds Agent content limit",
                retryable=False,
            )
        return RepositoryFile(
            path=value.path,
            content=bytes(value.content),
            content_hash=value.content_hash,
        )

    def search_prd_catalog(
        self,
        *,
        query: str,
        limit: int = 20,
    ) -> tuple[PrdCatalogHit, ...]:
        if not query.strip():
            raise ValueError("PRD catalog query is required")
        if not 1 <= limit <= 100:
            raise ValueError("PRD catalog limit must be between 1 and 100")
        response = self._call(
            self._stub.SearchPrdCatalog,
            capability.SearchPrdCatalogRequest(
                capability=self._capability("search-prd-catalog"),
                query=query,
                limit=limit,
            ),
        )
        return tuple(
            PrdCatalogHit(
                locator_id=item.locator_id,
                source_revision=item.source_revision,
                title=item.title,
                excerpt=item.excerpt,
            )
            for item in response.hits
        )

    def fetch_prd_sections(
        self,
        locator_ids,
    ) -> tuple[PrdSection, ...]:
        values = tuple(dict.fromkeys(str(item) for item in locator_ids if str(item)))
        if not values:
            raise ValueError("at least one PRD locator is required")
        if len(values) > 50:
            raise ValueError("too many PRD locators")
        response = self._call(
            self._stub.FetchPrdSections,
            capability.FetchPrdSectionsRequest(
                capability=self._capability("fetch-prd-sections"),
                locator_ids=values,
            ),
        )
        sections = tuple(
            PrdSection(
                locator_id=item.locator_id,
                source_revision=item.source_revision,
                title=item.title,
                markdown=item.markdown,
            )
            for item in response.sections
        )
        if sum(len(item.markdown.encode("utf-8")) for item in sections) > self._max_content_bytes:
            raise CapabilityError(
                "CONTENT_LIMIT_EXCEEDED",
                "PRD sections exceed Agent content limit",
                retryable=False,
            )
        return sections

    def close(self) -> None:
        channel = getattr(self, "_channel", None)
        if channel is not None:
            channel.close()

    def _capability(self, operation: str) -> capability.CapabilityLease:
        sequence = next(self._counter)
        return capability.CapabilityLease(
            lease=self._session.lease.as_proto(),
            contract_version=CONTRACT_VERSION,
            meta=execution.RequestMeta(
                contract_version=CONTRACT_VERSION,
                request_id=f"{self._session.request_id_prefix}:{operation}:{sequence}",
                correlation_id=self._session.correlation_id,
            ),
        )

    def _call(self, method, request):
        try:
            return method(request, timeout=self._timeout)
        except grpc.RpcError as error:
            code = error.code().name if error.code() else "UNKNOWN"
            detail = error.details() or "capability RPC failed"
            retryable = code in {
                "UNAVAILABLE",
                "DEADLINE_EXCEEDED",
                "RESOURCE_EXHAUSTED",
                "ABORTED",
            }
            raise CapabilityError(code, detail, retryable=retryable) from error
