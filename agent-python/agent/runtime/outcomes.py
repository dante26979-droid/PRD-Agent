from __future__ import annotations

import json
from typing import Any, Callable, TypeVar

from agent.v1 import agent_execution_pb2 as proto

from .idempotency import canonical_json
import hashlib

T = TypeVar("T")


def encode_outcome(schema: str, value: Any) -> bytes:
    return canonical_json({"schema": schema, "value": value})


def decode_outcome(content: bytes, schema: str, validate: Callable[[Any], T]) -> T:
    try:
        envelope = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("outcome artifact is not canonical JSON") from error
    if envelope.get("schema") != schema or "value" not in envelope:
        raise ValueError("outcome artifact schema mismatch")
    if canonical_json(envelope) != content:
        raise ValueError("outcome artifact is not canonically encoded")
    return validate(envelope["value"])


def build_artifact(*, key: str, artifact_type: str, request_hash: str, schema: str, value: Any) -> proto.RunArtifact:
    content = encode_outcome(schema, value)
    return proto.RunArtifact(artifact_key=key, artifact_type=artifact_type, generation=1, request_hash=request_hash, content_hash=hashlib.sha256(content).hexdigest(), content=content)
