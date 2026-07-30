from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping


CHECKPOINT_SCHEMA_VERSION = "agent-checkpoint.v1"
MAX_CHECKPOINT_BYTES = 256 * 1024


class CheckpointError(ValueError):
    """Raised when an Agent checkpoint cannot be trusted or resumed."""


@dataclass(frozen=True)
class CheckpointState:
    schema_version: str
    workflow_version: str
    run_id: str
    task_version: int
    sequence: int
    payload: Mapping[str, Any]
    payload_hash: str


class CheckpointCodec:
    """Version and integrity check Python-owned opaque checkpoint payloads."""

    def encode(
        self,
        *,
        workflow_version: str,
        run_id: str,
        task_version: int,
        sequence: int,
        payload: Mapping[str, Any],
    ) -> bytes:
        if not workflow_version or not run_id:
            raise CheckpointError("workflow_version and run_id are required")
        if task_version < 0 or sequence < 1:
            raise CheckpointError("task_version and sequence are invalid")
        payload_json = _canonical_json(payload)
        envelope = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "workflow_version": workflow_version,
            "run_id": run_id,
            "task_version": task_version,
            "sequence": sequence,
            "payload": dict(payload),
            "payload_hash": "sha256:" + hashlib.sha256(payload_json).hexdigest(),
        }
        encoded = _canonical_json(envelope)
        if len(encoded) > MAX_CHECKPOINT_BYTES:
            raise CheckpointError("checkpoint exceeds size limit")
        return encoded

    def decode(self, checkpoint: bytes) -> CheckpointState:
        if not checkpoint:
            raise CheckpointError("checkpoint is empty")
        if len(checkpoint) > MAX_CHECKPOINT_BYTES:
            raise CheckpointError("checkpoint exceeds size limit")
        try:
            value = json.loads(checkpoint)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CheckpointError("checkpoint is not valid JSON") from error
        if not isinstance(value, dict):
            raise CheckpointError("checkpoint envelope must be an object")
        if value.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointError("checkpoint schema is incompatible")
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise CheckpointError("checkpoint payload must be an object")
        expected_hash = "sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest()
        if value.get("payload_hash") != expected_hash:
            raise CheckpointError("checkpoint payload hash mismatch")
        try:
            state = CheckpointState(
                schema_version=str(value["schema_version"]),
                workflow_version=str(value["workflow_version"]),
                run_id=str(value["run_id"]),
                task_version=int(value["task_version"]),
                sequence=int(value["sequence"]),
                payload=payload,
                payload_hash=expected_hash,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise CheckpointError("checkpoint envelope is incomplete") from error
        if not state.workflow_version or not state.run_id or state.sequence < 1:
            raise CheckpointError("checkpoint envelope is invalid")
        return state


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
