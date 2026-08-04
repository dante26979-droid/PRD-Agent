from __future__ import annotations

import hashlib

from agent.runtime.outcomes import decode_outcome
from agent.v1 import agent_execution_pb2 as proto

from .module import _validate_bundle


def decode_memory_bundle_artifact(artifact: proto.RunArtifact) -> dict[str, object]:
    if artifact.artifact_type != "PROJECT_MEMORY_BUNDLE":
        raise ValueError("artifact is not a Project Memory Bundle")
    if artifact.content_hash != hashlib.sha256(artifact.content).hexdigest():
        raise ValueError("Project Memory Bundle artifact hash mismatch")
    return decode_outcome(artifact.content, "memory-bundle.v1", _validate_bundle)
