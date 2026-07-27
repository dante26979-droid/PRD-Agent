from __future__ import annotations

import re
import subprocess

from .bindings import RepositoryCatalog
from .errors import BlobNotFound, BlockedPath, InvalidRevision, RepositoryError
from .models import BlobContent, BlobEntry, RepositorySnapshot
from .path_policy import RepositoryPathPolicy


_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")


class GitCliObjectReader:
    def __init__(self, catalog: RepositoryCatalog, *, timeout_seconds: float = 5.0) -> None:
        self.catalog = catalog
        self.timeout_seconds = timeout_seconds
        self.path_policy = RepositoryPathPolicy()

    def resolve_snapshot(
        self, repository_id: str, revision: str | None = None
    ) -> RepositorySnapshot:
        binding = self.catalog.get(repository_id)
        candidate = binding.default_revision if revision is None else revision
        if not isinstance(candidate, str) or not _REVISION.fullmatch(candidate):
            raise InvalidRevision("revision contains unsupported characters")
        try:
            output = self._git(binding.root_path, "rev-parse", "--verify", f"{candidate}^{{commit}}")
        except RepositoryError as exc:
            raise InvalidRevision("revision does not resolve to a commit") from exc
        sha = output.decode("ascii").strip().lower()
        if not _SHA.fullmatch(sha):
            raise InvalidRevision("revision did not resolve to a full commit SHA")
        return RepositorySnapshot(repository_id, sha, binding.allowed_prefix)

    def read_blob(self, snapshot: RepositorySnapshot, path: str) -> BlobContent:
        binding = self.catalog.get(snapshot.repository_id)
        relative = self.path_policy.validate(path)
        full_path = f"{snapshot.allowed_prefix}/{relative}" if snapshot.allowed_prefix else relative
        try:
            entry = self._git(
                binding.root_path,
                "ls-tree",
                "-z",
                snapshot.resolved_commit_sha,
                "--",
                full_path,
            )
            if not entry:
                raise BlobNotFound(f"blob does not exist in snapshot: {relative}")
            mode = entry.split(maxsplit=1)[0].decode("ascii")
            if mode == "120000":
                raise BlockedPath("git symlink entries are blocked")
            blob_id = self._git(
                binding.root_path,
                "rev-parse",
                "--verify",
                f"{snapshot.resolved_commit_sha}:{full_path}",
            ).decode("ascii").strip()
            data = self._git(
                binding.root_path,
                "show",
                f"{snapshot.resolved_commit_sha}:{full_path}",
            )
        except BlockedPath:
            raise
        except RepositoryError as exc:
            raise BlobNotFound(f"blob does not exist in snapshot: {relative}") from exc
        return BlobContent(relative, blob_id, data)

    def list_blobs(self, snapshot: RepositorySnapshot) -> tuple[BlobEntry, ...]:
        binding = self.catalog.get(snapshot.repository_id)
        raw = self._git(
            binding.root_path,
            "ls-tree",
            "-r",
            "-l",
            "-z",
            "--full-tree",
            snapshot.resolved_commit_sha,
        )
        prefix = snapshot.allowed_prefix + "/" if snapshot.allowed_prefix else ""
        entries: list[BlobEntry] = []
        for record in raw.split(b"\x00"):
            if not record or b"\t" not in record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            parts = metadata.decode("ascii").split()
            if len(parts) != 4 or parts[1] != "blob":
                continue
            full_path = raw_path.decode("utf-8")
            if prefix and not full_path.startswith(prefix):
                continue
            relative = full_path[len(prefix) :] if prefix else full_path
            entries.append(
                BlobEntry(
                    path=relative,
                    blob_id=parts[2],
                    size=int(parts[3]),
                    mode=parts[0],
                )
            )
        return tuple(sorted(entries, key=lambda item: item.path))

    def _git(self, root, *args: str) -> bytes:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *args],
                check=False,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepositoryError("git object operation failed") from exc
        if result.returncode != 0:
            raise RepositoryError("git object operation failed")
        return result.stdout
