from __future__ import annotations

import uuid

from prd_agent.evidence.models import (
    DeterministicFact,
    ExtractionMethod,
    FactScope,
    FactType,
    SourceEvidence,
    VerificationStatus,
)
from prd_agent.hashing import sha256_json
from prd_agent.sources.models import SourceBinding, SourceKind

from .models import HistoricalPrdResultItem
from .text import safe_excerpt


def evidence_from_result(
    item: HistoricalPrdResultItem,
    *,
    tool_call_id: str,
    binding: SourceBinding,
) -> SourceEvidence:
    if binding.source_kind != SourceKind.HISTORICAL_PRD_CORPUS:
        raise ValueError("historical evidence requires a historical corpus binding")
    excerpt, redacted = safe_excerpt(item.excerpt)
    locator = {
        "corpus_id": binding.source_id,
        "corpus_version": binding.source_version,
        "source_uri": item.source_uri,
        "section_path": list(item.section_path),
        "chunk_id": item.chunk_id,
    }
    return SourceEvidence(
        evidence_id="evidence-"
        + sha256_json(
            {
                "tool_call_id": tool_call_id,
                "chunk_id": item.chunk_id,
                "content_hash": item.content_hash,
            }
        ).split(":", 1)[1][:32],
        tool_call_id=tool_call_id,
        source_type="HISTORICAL_PRD",
        source_kind=SourceKind.HISTORICAL_PRD_DOCUMENT,
        source_id=item.document_id,
        source_version=item.document_version_id,
        locator=locator,
        access_scope_hash=binding.access_scope_hash,
        excerpt=excerpt,
        content_hash=item.content_hash,
        extraction_method=ExtractionMethod.HISTORICAL_RETRIEVAL,
        redaction_applied=redacted,
    )


def historical_fact(
    *,
    evidence: SourceEvidence,
    task_id: str | None,
    subject: str,
    predicate: str,
    value: object,
) -> DeterministicFact:
    if evidence.source_kind != SourceKind.HISTORICAL_PRD_DOCUMENT:
        raise ValueError("historical facts require historical evidence")
    return DeterministicFact(
        fact_id=f"fact-{uuid.uuid4().hex}",
        tool_call_id=evidence.tool_call_id,
        task_id=task_id,
        subject=subject,
        predicate=predicate,
        value_json=value,
        fact_scope=FactScope.HISTORICAL_CONTEXT,
        fact_type=FactType.DOCUMENT_SUPPORTED,
        confidence="HIGH",
        verification_status=VerificationStatus.SUPPORTED,
        extractor_id="historical_prd_search",
        extractor_version="1",
        evidence_ids=(evidence.evidence_id,),
    )
