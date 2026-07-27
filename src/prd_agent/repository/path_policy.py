from __future__ import annotations

from pathlib import PurePosixPath

from .bindings import normalize_relative_path
from .errors import BlockedPath


class RepositoryPathPolicy:
    _blocked_segments = frozenset(
        {".git", "node_modules", "vendor", ".venv", "venv", "__pycache__"}
    )
    _blocked_names = frozenset(
        {".env", "id_rsa", "id_ed25519", "credentials", "credentials.json"}
    )
    _blocked_suffixes = (".pem", ".key", ".p12", ".pfx")

    def validate(self, value: str, *, allow_empty: bool = False) -> str:
        normalized = normalize_relative_path(value, allow_empty=allow_empty)
        if not normalized:
            return normalized
        path = PurePosixPath(normalized)
        lowered = tuple(part.lower() for part in path.parts)
        if any(part in self._blocked_segments for part in lowered):
            raise BlockedPath("path is blocked by repository policy")
        name = path.name.lower()
        if (
            name in self._blocked_names
            or name.startswith(".env.")
            or name.endswith(self._blocked_suffixes)
        ):
            raise BlockedPath("path is blocked by repository policy")
        return normalized

    def allowed(self, value: str) -> bool:
        try:
            self.validate(value)
        except BlockedPath:
            return False
        return True

