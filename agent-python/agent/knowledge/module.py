from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import asdict, replace

from agent.action_identity import action_signature

from .models import (
    CoverageUpdate,
    EvidenceRecord,
    FactScope,
    FactType,
    KnowledgeBuildRequest,
    KnowledgeBuildResult,
    KnowledgeBundle,
    SourceAuthority,
    SourceConflict,
    Unknown,
    VerificationStatus,
    VerifiedFact,
)


class EvidenceKnowledgeModule:
    """Validate capability evidence and project semantic investigation progress."""

    def build(self, request: KnowledgeBuildRequest) -> KnowledgeBuildResult:
        signature = action_signature(request.action)
        prior = request.prior_bundle
        evidence = {item.evidence_id: item for item in prior.evidence} if prior else {}
        facts = {item.fact_id: item for item in prior.facts} if prior else {}
        unknowns = {item.unknown_id: item for item in prior.unknowns} if prior else {}
        valid_ids: list[str] = []
        invalid_reasons: list[str] = []

        for item in request.evidence_items:
            authority, reason = _validate_source(item, request.source_authorities)
            if authority is None:
                invalid_reasons.append(reason)
                continue
            record = _evidence_record(item, authority, signature)
            evidence[record.evidence_id] = record
            valid_ids.append(record.evidence_id)

        extracted = _extract_facts(
            request,
            tuple(evidence[item] for item in valid_ids),
        )
        for fact in extracted:
            facts[fact.fact_id] = fact
        detected_facts, conflicts = _detect_conflicts(tuple(facts.values()))
        facts = {item.fact_id: item for item in detected_facts}

        coverage = dict(request.coverage)
        updates: list[CoverageUpdate] = []
        for key in request.action.target_coverage:
            before = coverage.get(key, "MISSING")
            supported = tuple(
                sorted(
                    fact.fact_id
                    for fact in facts.values()
                    if fact.verification_status is VerificationStatus.SUPPORTED
                    and key in fact.coverage_keys
                )
            )
            blocking_conflicts = tuple(
                sorted(
                    conflict.conflict_id
                    for conflict in conflicts
                    if any(
                        key in facts[fact_id].coverage_keys
                        for fact_id in conflict.fact_ids
                    )
                )
            )
            new_unknowns: tuple[str, ...] = ()
            if blocking_conflicts:
                after = "CONFLICTING"
                reason_code = "OPEN_SOURCE_CONFLICT"
            elif supported:
                after = "COVERED"
                reason_code = "SUPPORTED_FACTS_AVAILABLE"
            else:
                after = before
                reason_code = invalid_reasons[0] if invalid_reasons else (
                    "EMPTY_RESULT" if not request.evidence_items else "UNSUPPORTED_EVIDENCE"
                )
                unknown = _unknown_for(
                    request,
                    key,
                    reason_code,
                    signature,
                    tuple(valid_ids),
                )
                unknowns[unknown.unknown_id] = unknown
                new_unknowns = (unknown.unknown_id,)
            coverage[key] = after
            updates.append(
                CoverageUpdate(
                    coverage_key=key,
                    before=before,
                    after=after,
                    supported_fact_ids=supported,
                    unknown_ids=new_unknowns,
                    conflict_ids=blocking_conflicts,
                    reason_code=reason_code,
                )
            )

        ordered_evidence = tuple(sorted(evidence.values(), key=lambda item: item.evidence_id))
        ordered_facts = tuple(sorted(facts.values(), key=lambda item: item.fact_id))
        ordered_unknowns = tuple(sorted(unknowns.values(), key=lambda item: item.unknown_id))
        ordered_conflicts = tuple(sorted(conflicts, key=lambda item: item.conflict_id))
        knowledge_fingerprint = _hash(
            {
                "evidence": [asdict(item) for item in ordered_evidence],
                "facts": [asdict(item) for item in ordered_facts],
                "unknowns": [asdict(item) for item in ordered_unknowns],
                "conflicts": [asdict(item) for item in ordered_conflicts],
            }
        )
        progress_fingerprint = _hash(
            {
                "supported_fact_ids": [
                    item.fact_id
                    for item in ordered_facts
                    if item.verification_status is VerificationStatus.SUPPORTED
                ],
                "unknown_ids": [item.unknown_id for item in ordered_unknowns],
                "conflict_ids": [item.conflict_id for item in ordered_conflicts],
                "coverage": sorted(coverage.items()),
            }
        )
        bundle_id = "knowledge-" + knowledge_fingerprint.removeprefix("sha256:")[:24]
        bundle = KnowledgeBundle(
            schema_version="knowledge-bundle.v1",
            bundle_id=bundle_id,
            run_id=request.run_id,
            task_id=request.task_id,
            need_plan_id=request.need_plan_id,
            need_context_hash=request.need_context_hash,
            evidence=ordered_evidence,
            facts=ordered_facts,
            unknowns=ordered_unknowns,
            conflicts=ordered_conflicts,
            coverage_updates=tuple(updates),
            knowledge_fingerprint=knowledge_fingerprint,
            progress_fingerprint=progress_fingerprint,
        )
        return KnowledgeBuildResult(
            bundle=bundle,
            coverage=coverage,
            progressed=prior is None or prior.progress_fingerprint != progress_fingerprint,
        )


def _validate_source(item, authorities: tuple[SourceAuthority, ...]):
    actual_hash = "sha256:" + hashlib.sha256(item.excerpt.encode("utf-8")).hexdigest()
    if actual_hash != item.excerpt_hash:
        return None, "HASH_MISMATCH"
    item_kind = item.source_kind or item.source_type
    item_binding = item.binding_id or item.source_id
    for authority in authorities:
        if item_kind != authority.source_kind or item_binding not in {
            authority.source_id,
            authority.binding_id,
        }:
            continue
        if item.source_version and item.source_version != authority.source_version:
            return None, "STALE_SOURCE"
        if (
            item.access_scope_hash
            and item.access_scope_hash != authority.access_scope_hash
        ):
            return None, "ACCESS_SCOPE_MISMATCH"
        if authority.source_kind == "github":
            prefix = f"github://{authority.binding_id}@{authority.source_version}/"
            if not item.locator.startswith(prefix):
                stale_prefix = f"github://{authority.binding_id}@"
                return None, (
                    "STALE_SOURCE" if item.locator.startswith(stale_prefix) else "LOCATOR_MISMATCH"
                )
        return authority, "VALID"
    return None, "INVALID_SOURCE"


def _evidence_record(item, authority: SourceAuthority, signature: str) -> EvidenceRecord:
    identity = {
        "source_kind": authority.source_kind,
        "binding_id": authority.binding_id,
        "source_id": authority.source_id,
        "source_version": authority.source_version,
        "access_scope_hash": authority.access_scope_hash,
        "locator": item.locator,
        "excerpt_hash": item.excerpt_hash,
    }
    return EvidenceRecord(
        evidence_id="evidence-" + _hash(identity).removeprefix("sha256:")[:24],
        source_kind=authority.source_kind,
        binding_id=authority.binding_id,
        source_id=authority.source_id,
        source_version=authority.source_version,
        access_scope_hash=authority.access_scope_hash,
        locator=item.locator,
        excerpt_hash=item.excerpt_hash,
        excerpt=item.excerpt,
        validation_status="VALID",
        action_signature=signature,
    )


def _directly_supported(query: str, evidence: tuple[EvidenceRecord, ...]) -> tuple[EvidenceRecord, ...]:
    tokens = tuple(token for token in _tokens(query) if len(token) > 1)
    if not tokens:
        return ()
    return tuple(
        item
        for item in evidence
        if all(token in _tokens(item.excerpt) for token in tokens)
    )


def _extract_facts(
    request: KnowledgeBuildRequest,
    evidence: tuple[EvidenceRecord, ...],
) -> tuple[VerifiedFact, ...]:
    if request.action.tool_id in {"parse_openapi", "parse_database_schema"}:
        return tuple(
            fact
            for record in evidence
            if (fact := _parser_fact(request, record)) is not None
        )
    return tuple(
        _direct_fact(request, record)
        for record in _directly_supported(
            str(request.action.arguments.get("query", "")), evidence
        )
    )


def _direct_fact(
    request: KnowledgeBuildRequest,
    evidence: EvidenceRecord,
) -> VerifiedFact:
    is_code = evidence.source_kind == "github"
    payload = {
        "subject": request.action.target_coverage[0],
        "predicate": "direct_source_support",
        "value": evidence.excerpt,
        "scope": "CURRENT_STATE" if is_code else "HISTORICAL_CONTEXT",
        "type": "CODE_VERIFIED" if is_code else "DOCUMENT_SUPPORTED",
        "evidence_ids": [evidence.evidence_id],
        "extractor": "direct-token-match.v1",
    }
    return VerifiedFact(
        fact_id="fact-" + _hash(payload).removeprefix("sha256:")[:24],
        subject=request.action.target_coverage[0],
        predicate="direct_source_support",
        value=evidence.excerpt,
        fact_scope=FactScope.CURRENT_STATE if is_code else FactScope.HISTORICAL_CONTEXT,
        fact_type=FactType.CODE_VERIFIED if is_code else FactType.DOCUMENT_SUPPORTED,
        verification_status=VerificationStatus.SUPPORTED,
        evidence_ids=(evidence.evidence_id,),
        coverage_keys=request.action.target_coverage,
        extractor_id="direct-token-match",
        extractor_version="1",
    )


def _parser_fact(
    request: KnowledgeBuildRequest,
    evidence: EvidenceRecord,
) -> VerifiedFact | None:
    try:
        raw = json.loads(evidence.excerpt)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "subject",
        "predicate",
        "value",
        "scope",
    }:
        return None
    try:
        scope = FactScope(str(raw["scope"]))
    except ValueError:
        return None
    fact_type = (
        FactType.CODE_VERIFIED
        if evidence.source_kind in {"github", "openapi", "database_schema"}
        else FactType.DOCUMENT_SUPPORTED
    )
    payload = {
        "subject": str(raw["subject"]),
        "predicate": str(raw["predicate"]),
        "value": raw["value"],
        "scope": scope.value,
        "type": fact_type.value,
        "evidence_ids": [evidence.evidence_id],
        "coverage_keys": list(request.action.target_coverage),
        "extractor": f"{request.action.tool_id}.v1",
    }
    return VerifiedFact(
        fact_id="fact-" + _hash(payload).removeprefix("sha256:")[:24],
        subject=str(raw["subject"]),
        predicate=str(raw["predicate"]),
        value=raw["value"],
        fact_scope=scope,
        fact_type=fact_type,
        verification_status=VerificationStatus.SUPPORTED,
        evidence_ids=(evidence.evidence_id,),
        coverage_keys=request.action.target_coverage,
        extractor_id=request.action.tool_id,
        extractor_version="1",
    )


def _detect_conflicts(
    facts: tuple[VerifiedFact, ...],
) -> tuple[tuple[VerifiedFact, ...], tuple[SourceConflict, ...]]:
    grouped: dict[tuple[str, str, FactScope], list[VerifiedFact]] = defaultdict(list)
    for fact in facts:
        if fact.extractor_id in {"parse_openapi", "parse_database_schema"}:
            grouped[(fact.subject, fact.predicate, fact.fact_scope)].append(fact)
    conflicting: set[str] = set()
    conflicts: list[SourceConflict] = []
    for (subject, predicate, _scope), group in grouped.items():
        if len({_hash(item.value) for item in group}) < 2:
            continue
        fact_ids = tuple(sorted(item.fact_id for item in group))
        conflicting.update(fact_ids)
        evidence_ids = tuple(
            sorted({value for item in group for value in item.evidence_ids})
        )
        identity = {
            "subject": subject,
            "predicate": predicate,
            "fact_ids": fact_ids,
        }
        conflicts.append(
            SourceConflict(
                conflict_id="conflict-"
                + _hash(identity).removeprefix("sha256:")[:24],
                subject=subject,
                predicate=predicate,
                fact_ids=fact_ids,
                evidence_ids=evidence_ids,
            )
        )
    return (
        tuple(
            replace(fact, verification_status=VerificationStatus.CONFLICTING)
            if fact.fact_id in conflicting
            else fact
            for fact in facts
        ),
        tuple(conflicts),
    )


def _unknown_for(
    request: KnowledgeBuildRequest,
    coverage_key: str,
    reason: str,
    signature: str,
    evidence_ids: tuple[str, ...],
) -> Unknown:
    identity = {
        "need_plan_id": request.need_plan_id,
        "coverage_key": coverage_key,
        "reason": reason,
        "action_signature": signature,
        "evidence_ids": sorted(evidence_ids),
    }
    return Unknown(
        unknown_id="unknown-" + _hash(identity).removeprefix("sha256:")[:24],
        coverage_key=coverage_key,
        question=str(request.action.arguments.get("query", "")),
        reason_code=reason,
        action_signature=signature,
        evidence_ids=tuple(sorted(evidence_ids)),
    )


def _tokens(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9\u4e00-\u9fff]+", value.lower().replace("_", " ")))


def _hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.value if hasattr(item, "value") else str(item),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
