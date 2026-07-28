from __future__ import annotations

import re
from typing import Iterable, Mapping

from prd_agent.integrations.errors import IntegrationError
from prd_agent.repository.bindings import normalize_relative_path
from prd_agent.repository.errors import (
    BlobNotFound,
    BlockedPath,
    InvalidRevision,
    UnknownRepository,
)
from prd_agent.repository.models import BlobContent, BlobEntry, RepositorySnapshot

from .gateway import RemoteRepositoryGateway
from .models import RemoteProvider, RemoteRepositoryBinding


_SHA = re.compile(r"[0-9a-f]{40}\Z")


class RemoteRepositoryCatalog:
    def __init__(self, bindings: Iterable[RemoteRepositoryBinding]) -> None:
        self._bindings = {binding.repository_id: binding for binding in bindings}

    def get(self, repository_id: str) -> RemoteRepositoryBinding:
        binding = self._bindings.get(repository_id)
        if binding is None or not binding.enabled:
            raise UnknownRepository(f"repository is not available: {repository_id}")
        return binding

    def list_for_owner(self, owner_id: str) -> tuple[RemoteRepositoryBinding, ...]:
        return tuple(
            sorted(
                (
                    binding
                    for binding in self._bindings.values()
                    if binding.owner_id == owner_id and binding.enabled
                ),
                key=lambda item: item.repository_id,
            )
        )


class RemoteRepositoryObjectReader:
    """Expose remote providers through the existing immutable object-reader contract."""

    def __init__(
        self,
        catalog: RemoteRepositoryCatalog,
        gateways: Mapping[RemoteProvider | str, RemoteRepositoryGateway],
    ) -> None:
        self.catalog = catalog
        self.gateways = {
            RemoteProvider(provider): gateway for provider, gateway in gateways.items()
        }

    def resolve_snapshot(
        self,
        repository_id: str,
        revision: str | None = None,
    ) -> RepositorySnapshot:
        binding = self.catalog.get(repository_id)
        gateway = self._gateway(binding)
        candidate = binding.default_revision if revision is None else revision
        if not isinstance(candidate, str) or not candidate.strip():
            raise InvalidRevision("revision is required")
        try:
            commit_sha = gateway.resolve_commit(binding, candidate).lower()
        except IntegrationError:
            raise
        except Exception as exc:
            raise InvalidRevision("revision does not resolve to a commit") from exc
        if not _SHA.fullmatch(commit_sha):
            raise InvalidRevision("revision did not resolve to a full commit SHA")
        return RepositorySnapshot(
            repository_id=binding.repository_id,
            resolved_commit_sha=commit_sha,
            allowed_prefix=binding.allowed_prefix,
            resolver_version=f"remote-{binding.provider.value.lower()}.v1",
        )

    def read_blob(self, snapshot: RepositorySnapshot, path: str) -> BlobContent:
        binding = self.catalog.get(snapshot.repository_id)
        relative = normalize_relative_path(path)
        full_path = self._full_path(binding.allowed_prefix, relative)
        try:
            blob = self._gateway(binding).read_blob(
                binding,
                snapshot.resolved_commit_sha,
                full_path,
            )
        except IntegrationError:
            raise
        except (BlockedPath, BlobNotFound):
            raise
        except Exception as exc:
            raise BlobNotFound(f"blob does not exist in snapshot: {relative}") from exc
        return BlobContent(path=relative, blob_id=blob.blob_id, data=blob.data)

    def list_blobs(self, snapshot: RepositorySnapshot) -> tuple[BlobEntry, ...]:
        binding = self.catalog.get(snapshot.repository_id)
        prefix = binding.allowed_prefix + "/" if binding.allowed_prefix else ""
        values: list[BlobEntry] = []
        for entry in self._gateway(binding).list_blobs(
            binding,
            snapshot.resolved_commit_sha,
        ):
            if entry.mode == "120000":
                continue
            if prefix and not entry.path.startswith(prefix):
                continue
            relative = entry.path[len(prefix) :] if prefix else entry.path
            try:
                relative = normalize_relative_path(relative)
            except BlockedPath:
                continue
            values.append(
                BlobEntry(
                    path=relative,
                    blob_id=entry.blob_id,
                    size=entry.size,
                    mode=entry.mode,
                )
            )
        return tuple(sorted(values, key=lambda item: item.path))

    def _gateway(self, binding: RemoteRepositoryBinding) -> RemoteRepositoryGateway:
        gateway = self.gateways.get(binding.provider)
        if gateway is None:
            raise UnknownRepository(
                f"repository provider is not available: {binding.provider.value}"
            )
        return gateway

    @staticmethod
    def _full_path(prefix: str, relative: str) -> str:
        return f"{prefix}/{relative}" if prefix else relative
