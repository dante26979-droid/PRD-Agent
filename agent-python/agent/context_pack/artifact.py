from __future__ import annotations

import hashlib
from typing import Mapping

from agent.runtime.idempotency import request_hash
from agent.runtime.outcomes import decode_outcome
from agent.v1 import agent_execution_pb2 as proto


def validate_context_pack_value(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("Context Pack must be an object")
    required = {
        "schema_version",
        "pack_id",
        "run_id",
        "task_id",
        "operation",
        "operation_sequence",
        "prompt_version",
        "output_schema",
        "policy_version",
        "source_manifest",
        "operation_view",
        "omitted_sources",
        "token_accounting",
        "compaction_kind",
    }
    if set(value) != required or value.get("schema_version") != "context-pack.v1":
        raise ValueError("Context Pack schema mismatch")
    if not isinstance(value.get("operation_view"), Mapping):
        raise ValueError("Context Pack operation_view must be an object")
    manifest = value.get("source_manifest")
    manifest_keys = {
        "schema_version",
        "run_id",
        "task_id",
        "operation",
        "sources",
        "manifest_hash",
    }
    if not isinstance(manifest, Mapping) or set(manifest) != manifest_keys:
        raise ValueError("Context Pack source manifest is invalid")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or any(
        not isinstance(item, Mapping)
        or set(item)
        != {
            "source_ref",
            "source_kind",
            "content_hash",
            "trust_class",
            "required",
            "priority",
        }
        or not str(item.get("content_hash", "")).startswith("sha256:")
        for item in sources
    ):
        raise ValueError("Context Pack source manifest is invalid")
    manifest_body = {
        key: item for key, item in manifest.items() if key != "manifest_hash"
    }
    if manifest.get("manifest_hash") != request_hash(manifest_body):
        raise ValueError("Context Pack source manifest hash mismatch")
    accounting = value.get("token_accounting")
    accounting_keys = {
        "estimated_tokens_before",
        "estimated_tokens_after",
        "target_input_tokens",
        "hard_input_tokens",
        "reserved_output_tokens",
        "emergency_margin_tokens",
    }
    if (
        not isinstance(accounting, Mapping)
        or set(accounting) != accounting_keys
        or any(not isinstance(item, int) or item < 0 for item in accounting.values())
    ):
        raise ValueError("Context Pack token accounting is invalid")
    omitted = value.get("omitted_sources")
    if not isinstance(omitted, list) or any(
        not isinstance(item, Mapping)
        or set(item) != {"source_ref", "reason"}
        or not item.get("source_ref")
        or not item.get("reason")
        for item in omitted
    ):
        raise ValueError("Context Pack omitted sources are invalid")
    seed = {
        "run_id": value["run_id"],
        "task_id": value["task_id"],
        "operation": value["operation"],
        "operation_sequence": value["operation_sequence"],
        "prompt_version": value["prompt_version"],
        "output_schema": value["output_schema"],
        "policy_version": value["policy_version"],
        "manifest_hash": manifest["manifest_hash"],
        "view_hash": request_hash(value["operation_view"]),
    }
    expected_pack_id = "context-" + request_hash(seed).removeprefix("sha256:")[:24]
    if value.get("pack_id") != expected_pack_id:
        raise ValueError("Context Pack identity mismatch")
    return dict(value)


def decode_context_pack_artifact(artifact: proto.RunArtifact) -> dict[str, object]:
    if artifact.artifact_type != "CONTEXT_PACK":
        raise ValueError("artifact is not a Context Pack")
    if artifact.content_hash != hashlib.sha256(artifact.content).hexdigest():
        raise ValueError("Context Pack artifact hash mismatch")
    return decode_outcome(
        artifact.content,
        "context-pack.v1",
        validate_context_pack_value,
    )
