from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class RepositorySnapshot:
    repository_id: str
    resolved_commit_sha: str
    allowed_prefix: str
    resolver_version: str = "git-cli.v1"
    resolved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class BlobEntry:
    path: str
    blob_id: str
    size: int
    mode: str


@dataclass(frozen=True)
class BlobContent:
    path: str
    blob_id: str
    data: bytes

    @property
    def text(self) -> str:
        return self.data.decode("utf-8")
