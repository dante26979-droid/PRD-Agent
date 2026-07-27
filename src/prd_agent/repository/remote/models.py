from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from prd_agent.repository.bindings import normalize_relative_path


class RemoteProvider(StrEnum):
    GITHUB = "GITHUB"
    GITLAB = "GITLAB"


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RemoteRepositoryBinding:
    repository_id: str
    owner_id: str
    connection_id: str
    provider: RemoteProvider
    provider_repository_id: str
    display_name: str
    default_revision: str
    access_scope_hash: str
    allowed_prefix: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        for name in (
            "repository_id",
            "owner_id",
            "connection_id",
            "provider_repository_id",
            "display_name",
            "default_revision",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        object.__setattr__(self, "provider", RemoteProvider(self.provider))
        object.__setattr__(
            self,
            "allowed_prefix",
            normalize_relative_path(self.allowed_prefix, allow_empty=True),
        )
        if not _SHA256.fullmatch(self.access_scope_hash):
            raise ValueError("access_scope_hash must be a sha256 digest")
