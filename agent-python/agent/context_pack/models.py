from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ContextSource:
    source_ref: str
    source_kind: str
    content_hash: str
    trust_class: str
    required: bool
    priority: int
    value: object

    def manifest_value(self) -> dict[str, object]:
        return {
            "source_ref": self.source_ref,
            "source_kind": self.source_kind,
            "content_hash": self.content_hash,
            "trust_class": self.trust_class,
            "required": self.required,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class SourceManifest:
    schema_version: str
    run_id: str
    task_id: str
    operation: str
    sources: tuple[ContextSource, ...]
    manifest_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "operation": self.operation,
            "sources": [item.manifest_value() for item in self.sources],
            "manifest_hash": self.manifest_hash,
        }


@dataclass(frozen=True)
class TokenAccounting:
    estimated_tokens_before: int
    estimated_tokens_after: int
    target_input_tokens: int
    hard_input_tokens: int
    reserved_output_tokens: int
    emergency_margin_tokens: int

    def as_dict(self) -> dict[str, int]:
        return {
            "estimated_tokens_before": self.estimated_tokens_before,
            "estimated_tokens_after": self.estimated_tokens_after,
            "target_input_tokens": self.target_input_tokens,
            "hard_input_tokens": self.hard_input_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "emergency_margin_tokens": self.emergency_margin_tokens,
        }


@dataclass(frozen=True)
class ContextPack:
    schema_version: str
    pack_id: str
    run_id: str
    task_id: str
    operation: str
    operation_sequence: int
    prompt_version: str
    output_schema: str
    policy_version: str
    source_manifest: SourceManifest
    operation_view: Mapping[str, object]
    omitted_sources: tuple[Mapping[str, str], ...]
    token_accounting: TokenAccounting
    compaction_kind: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "pack_id": self.pack_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "operation": self.operation,
            "operation_sequence": self.operation_sequence,
            "prompt_version": self.prompt_version,
            "output_schema": self.output_schema,
            "policy_version": self.policy_version,
            "source_manifest": self.source_manifest.as_dict(),
            "operation_view": dict(self.operation_view),
            "omitted_sources": [dict(item) for item in self.omitted_sources],
            "token_accounting": self.token_accounting.as_dict(),
            "compaction_kind": self.compaction_kind,
        }


@dataclass(frozen=True)
class PreparedModelContext:
    payload: Mapping[str, object]
    canonical_payload: bytes
    request_hash: str
    estimated_input_tokens: int
    context_pack: ContextPack | None = None
    context_pack_artifact_key: str = ""
    context_pack_artifact_hash: str = ""
    shadow_payload: Mapping[str, object] | None = None

    @property
    def compacted(self) -> bool:
        return self.context_pack is not None and self.context_pack.compaction_kind != "NONE"
