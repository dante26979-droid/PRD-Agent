from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_SAFE_OPERATION_KEY = re.compile(r"^[a-z][a-z0-9_.-]*(?::[a-z0-9_.-]+){2,8}$")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def tagged_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def request_hash(value: Any) -> str:
    return tagged_sha256(canonical_json(value))


def outcome_artifact_key(run_id: str, operation_key: str) -> str:
    digest = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:32]
    return f"{run_id}:ledger:{digest}:outcome"


def validate_operation_key(value: str) -> str:
    if len(value) > 240 or not _SAFE_OPERATION_KEY.fullmatch(value):
        raise ValueError("operation_key must be a bounded low-cardinality identity")
    return value
