from __future__ import annotations

from agent.checkpoint import CheckpointError
from agent.resume.hashes import HashDigest


def index_artifacts(items) -> dict[str, object]:
    indexed: dict[str, object] = {}
    for item in items:
        if not item.artifact_key or item.artifact_key in indexed:
            raise CheckpointError("artifact index contains an invalid or duplicate key")
        if not item.artifact_type or item.generation < 0 or not item.request_hash:
            raise CheckpointError("artifact identity is incomplete")
        expected = HashDigest.parse(item.content_hash)
        if expected != HashDigest.of_bytes(item.content):
            raise CheckpointError("artifact content hash mismatch")
        indexed[item.artifact_key] = item
    return indexed
