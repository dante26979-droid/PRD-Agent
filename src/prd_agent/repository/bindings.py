from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .errors import BlockedPath, UnknownRepository


def normalize_relative_path(value: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise BlockedPath("path must be a string")
    if "\x00" in value or "\\" in value:
        raise BlockedPath("path contains blocked characters")
    if value == "":
        if allow_empty:
            return ""
        raise BlockedPath("path cannot be empty")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BlockedPath("path must be a normalized relative POSIX path")
    normalized = path.as_posix()
    if normalized == "." or normalized != value:
        raise BlockedPath("path must be a normalized relative POSIX path")
    return normalized


@dataclass(frozen=True)
class RepositoryBinding:
    repository_id: str
    root_path: Path
    allowed_prefix: str
    default_revision: str
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.repository_id.strip():
            raise ValueError("repository_id is required")
        object.__setattr__(self, "root_path", self.root_path.resolve())
        object.__setattr__(
            self,
            "allowed_prefix",
            normalize_relative_path(self.allowed_prefix, allow_empty=True),
        )


class RepositoryCatalog:
    def __init__(self, bindings: Iterable[RepositoryBinding]) -> None:
        self._bindings = {item.repository_id: item for item in bindings}

    def get(self, repository_id: str) -> RepositoryBinding:
        binding = self._bindings.get(repository_id)
        if not binding or not binding.enabled:
            raise UnknownRepository(f"repository is not available: {repository_id}")
        return binding
