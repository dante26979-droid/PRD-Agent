from __future__ import annotations

from typing import Protocol

from prd_agent.repository.models import BlobContent, BlobEntry

from .models import RemoteRepositoryBinding


class RemoteRepositoryGateway(Protocol):
    def resolve_commit(
        self,
        binding: RemoteRepositoryBinding,
        revision: str,
    ) -> str: ...

    def list_blobs(
        self,
        binding: RemoteRepositoryBinding,
        commit_sha: str,
    ) -> tuple[BlobEntry, ...]: ...

    def read_blob(
        self,
        binding: RemoteRepositoryBinding,
        commit_sha: str,
        path: str,
    ) -> BlobContent: ...
