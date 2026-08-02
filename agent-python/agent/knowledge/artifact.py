from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from agent.v1 import agent_execution_pb2 as proto

from .models import (
    CoverageUpdate,
    EvidenceRecord,
    FactScope,
    FactType,
    KnowledgeBundle,
    SourceConflict,
    Unknown,
    VerificationStatus,
    VerifiedFact,
)


class KnowledgeArtifactCodec:
    def encode(
        self,
        bundle: KnowledgeBundle,
        *,
        generation: int,
    ) -> proto.RunArtifact:
        content = _canonical(asdict(bundle))
        return proto.RunArtifact(
            artifact_key=f"{bundle.run_id}:knowledge_bundle:{generation}",
            artifact_type="KNOWLEDGE_BUNDLE",
            generation=generation,
            request_hash=bundle.knowledge_fingerprint,
            content_hash=hashlib.sha256(content).hexdigest(),
            content=content,
        )

    def decode(self, artifact: proto.RunArtifact) -> KnowledgeBundle:
        if artifact.artifact_type != "KNOWLEDGE_BUNDLE":
            raise ValueError("artifact is not a Knowledge Bundle")
        if hashlib.sha256(artifact.content).hexdigest() != artifact.content_hash:
            raise ValueError("Knowledge Artifact content hash mismatch")
        try:
            raw = json.loads(artifact.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Knowledge Artifact is invalid JSON") from error
        if not isinstance(raw, dict) or _canonical(raw) != artifact.content:
            raise ValueError("Knowledge Artifact is not canonical")
        bundle = _bundle_from_dict(raw)
        if artifact.request_hash != bundle.knowledge_fingerprint:
            raise ValueError("Knowledge Artifact request hash mismatch")
        return bundle


def _bundle_from_dict(raw: dict[str, object]) -> KnowledgeBundle:
    evidence = tuple(EvidenceRecord(**item) for item in _objects(raw, "evidence"))
    facts = tuple(
        VerifiedFact(
            fact_id=str(item["fact_id"]),
            subject=str(item["subject"]),
            predicate=str(item["predicate"]),
            value=item["value"],
            fact_scope=FactScope(str(item["fact_scope"])),
            fact_type=FactType(str(item["fact_type"])),
            verification_status=VerificationStatus(
                str(item["verification_status"])
            ),
            evidence_ids=tuple(str(value) for value in item["evidence_ids"]),
            coverage_keys=tuple(str(value) for value in item["coverage_keys"]),
            extractor_id=str(item["extractor_id"]),
            extractor_version=str(item["extractor_version"]),
        )
        for item in _objects(raw, "facts")
    )
    unknowns = tuple(
        Unknown(
            unknown_id=str(item["unknown_id"]),
            coverage_key=str(item["coverage_key"]),
            question=str(item["question"]),
            reason_code=str(item["reason_code"]),
            action_signature=str(item["action_signature"]),
            evidence_ids=tuple(str(value) for value in item["evidence_ids"]),
        )
        for item in _objects(raw, "unknowns")
    )
    conflicts = tuple(
        SourceConflict(
            conflict_id=str(item["conflict_id"]),
            subject=str(item["subject"]),
            predicate=str(item["predicate"]),
            fact_ids=tuple(str(value) for value in item["fact_ids"]),
            evidence_ids=tuple(str(value) for value in item["evidence_ids"]),
            status=str(item["status"]),
        )
        for item in _objects(raw, "conflicts")
    )
    updates = tuple(
        CoverageUpdate(
            coverage_key=str(item["coverage_key"]),
            before=str(item["before"]),
            after=str(item["after"]),
            supported_fact_ids=tuple(
                str(value) for value in item["supported_fact_ids"]
            ),
            unknown_ids=tuple(str(value) for value in item["unknown_ids"]),
            conflict_ids=tuple(str(value) for value in item["conflict_ids"]),
            reason_code=str(item["reason_code"]),
        )
        for item in _objects(raw, "coverage_updates")
    )
    return KnowledgeBundle(
        schema_version=str(raw["schema_version"]),
        bundle_id=str(raw["bundle_id"]),
        run_id=str(raw["run_id"]),
        task_id=str(raw["task_id"]),
        need_plan_id=str(raw["need_plan_id"]),
        need_context_hash=str(raw["need_context_hash"]),
        evidence=evidence,
        facts=facts,
        unknowns=unknowns,
        conflicts=conflicts,
        coverage_updates=updates,
        knowledge_fingerprint=str(raw["knowledge_fingerprint"]),
        progress_fingerprint=str(raw["progress_fingerprint"]),
        builder_version=str(raw["builder_version"]),
    )


def _objects(raw: dict[str, object], key: str) -> tuple[dict[str, object], ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"Knowledge Artifact {key} must be an object array")
    return tuple(value)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.value if hasattr(item, "value") else str(item),
    ).encode("utf-8")
