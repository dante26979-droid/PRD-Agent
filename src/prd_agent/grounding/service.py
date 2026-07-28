from __future__ import annotations

import json

from prd_agent.evidence.models import FactScope, FactType, VerificationStatus
from prd_agent.sources.models import SourceKind

from .models import (
    ClaimCriticality,
    ClaimGroundingAssessment,
    ClaimKind,
    FactGroundingAssessment,
    GroundingAction,
    GroundingRequest,
    GroundingReference,
    GroundingResult,
    GroundingStatus,
    GroundingVerdict,
)


def _searchable_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip().lower()
    return json.dumps(value, ensure_ascii=False, sort_keys=True).lower()


class GroundingService:
    """Conservative deterministic grounding policy over verified repository facts."""

    def __init__(self, retry_provider=None) -> None:
        self.retry_provider = retry_provider

    def ground(self, request: GroundingRequest) -> GroundingResult:
        evidence_by_id = {item.evidence_id: item for item in request.evidence}
        conflict_fact_ids = {
            fact_id
            for conflict in request.conflicts
            if conflict.status == "OPEN"
            for fact_id in conflict.fact_ids
        }
        fact_assessments: list[FactGroundingAssessment] = []
        for fact in request.facts:
            verdict, reason = self._assess_fact(
                request,
                fact,
                evidence_by_id,
                conflict_fact_ids,
            )
            fact_assessments.append(
                FactGroundingAssessment(
                    fact_id=fact.fact_id,
                    evidence_ids=fact.evidence_ids,
                    verdict=verdict,
                    reason_code=reason,
                )
            )

        fact_verdicts = {item.fact_id: item.verdict for item in fact_assessments}
        claim_assessments: list[ClaimGroundingAssessment] = []
        used_fact_ids: list[str] = []
        issues: list[str] = []
        for claim in request.claims:
            assessment = self._assess_claim(
                claim,
                fact_verdicts,
                {item.fact_id: item for item in request.facts},
            )
            claim_assessments.append(assessment)
            used_fact_ids.extend(assessment.valid_fact_ids)
            if assessment.verdict != GroundingVerdict.SUPPORTED:
                issues.append(f"{claim.claim_id}:{assessment.reason_code}")

        claims_by_id = {claim.claim_id: claim for claim in request.claims}
        blocking = any(
            assessment.verdict != GroundingVerdict.SUPPORTED
            and claims_by_id[assessment.claim_id].kind
            in {
                ClaimKind.CURRENT_STATE,
                ClaimKind.HISTORICAL_CONTEXT,
                ClaimKind.TARGET_DECISION,
                ClaimKind.ASSUMPTION,
            }
            for assessment in claim_assessments
        )
        degraded = bool(issues) and not blocking
        status = (
            GroundingStatus.HUMAN_INPUT_REQUIRED
            if blocking
            else GroundingStatus.DEGRADED
            if degraded
            else GroundingStatus.PASSED
        )
        supported_fact_ids = {
            item.fact_id
            for item in fact_assessments
            if item.verdict == GroundingVerdict.SUPPORTED
        }
        fact_by_id = {item.fact_id: item for item in request.facts}
        references: list[GroundingReference] = []
        for fact_id in dict.fromkeys(used_fact_ids):
            if fact_id not in supported_fact_ids:
                continue
            fact = fact_by_id[fact_id]
            claim_ids = tuple(
                claim.claim_id
                for claim in request.claims
                if fact_id in claim.fact_ids
            )
            for evidence_id in fact.evidence_ids:
                evidence = evidence_by_id[evidence_id]
                references.append(
                    GroundingReference(
                        claim_ids=claim_ids,
                        fact_id=fact_id,
                        evidence_id=evidence_id,
                        source_kind=evidence.source_kind,
                        source_id=evidence.source_id,
                        source_version=evidence.source_version,
                        locator=evidence.locator,
                        repository_id=evidence.repository_id,
                        resolved_commit_sha=evidence.resolved_commit_sha,
                        path=evidence.path,
                        line_start=evidence.line_start,
                        line_end=evidence.line_end,
                        symbol=evidence.symbol,
                    )
                )
        result = GroundingResult(
            grounding_run_id=request.grounding_run_id,
            task_id=request.task_id,
            unit_id=request.unit_id,
            status=status,
            confirmable=not blocking,
            fact_assessments=tuple(fact_assessments),
            claim_assessments=tuple(claim_assessments),
            used_fact_ids=tuple(dict.fromkeys(used_fact_ids)),
            references=tuple(references),
            retry_count=request.retry_count,
            issues=tuple(issues),
        )
        if blocking and request.retry_count == 0 and self.retry_provider is not None:
            failed_claim_ids = tuple(
                assessment.claim_id
                for assessment in claim_assessments
                if assessment.verdict != GroundingVerdict.SUPPORTED
                and claims_by_id[assessment.claim_id].kind
                == ClaimKind.CURRENT_STATE
                and claims_by_id[assessment.claim_id].criticality
                != ClaimCriticality.INFORMATIONAL
            )
            if not failed_claim_ids:
                return result
            supplement = self.retry_provider.supplement(request, failed_claim_ids)
            if supplement is not None:
                facts = {item.fact_id: item for item in request.facts}
                facts.update({item.fact_id: item for item in supplement.facts})
                evidence = {item.evidence_id: item for item in request.evidence}
                evidence.update(
                    {item.evidence_id: item for item in supplement.evidence}
                )
                conflicts = {item.conflict_id: item for item in request.conflicts}
                conflicts.update(
                    {item.conflict_id: item for item in supplement.conflicts}
                )
                retried = request.model_copy(
                    update={
                        "facts": tuple(facts.values()),
                        "evidence": tuple(evidence.values()),
                        "conflicts": tuple(conflicts.values()),
                        "retry_count": 1,
                    }
                )
                return GroundingService().ground(retried)
        return result

    @staticmethod
    def _assess_fact(request, fact, evidence_by_id, conflict_fact_ids):
        if fact.task_id and fact.task_id != request.task_id:
            return GroundingVerdict.INVALID_SOURCE, "FACT_TASK_MISMATCH"
        if fact.fact_id in conflict_fact_ids:
            return GroundingVerdict.CONFLICTING, "OPEN_SOURCE_CONFLICT"
        if not fact.evidence_ids:
            return GroundingVerdict.INVALID_SOURCE, "EVIDENCE_REQUIRED"
        if fact.fact_type in {FactType.INFERRED, FactType.UNKNOWN}:
            return GroundingVerdict.UNSUPPORTED, "NON_DETERMINISTIC_FACT"
        if fact.verification_status == VerificationStatus.CONFLICTING:
            return GroundingVerdict.CONFLICTING, "FACT_CONFLICTING"
        if fact.verification_status == VerificationStatus.PARTIALLY_SUPPORTED:
            return GroundingVerdict.PARTIALLY_SUPPORTED, "FACT_PARTIAL"
        if fact.verification_status != VerificationStatus.SUPPORTED:
            return GroundingVerdict.UNSUPPORTED, "FACT_NOT_VERIFIED"
        linked = []
        for evidence_id in fact.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                return GroundingVerdict.INVALID_SOURCE, "EVIDENCE_NOT_FOUND"
            source_verdict = GroundingService._source_verdict(request, evidence)
            if source_verdict is not None:
                return source_verdict
            if not evidence.excerpt.strip() or not evidence.content_hash:
                return GroundingVerdict.INVALID_SOURCE, "EVIDENCE_NOT_REPRODUCIBLE"
            linked.append(evidence)
        value = _searchable_value(fact.value_json)
        if value and not any(value in item.excerpt.lower() for item in linked):
            return GroundingVerdict.UNSUPPORTED, "EVIDENCE_ONLY_RELATED"
        return GroundingVerdict.SUPPORTED, "DIRECT_SUPPORT"

    @staticmethod
    def _source_verdict(request, evidence):
        if request.allowed_source_bindings:
            candidates = [
                binding
                for binding in request.allowed_source_bindings
                if (
                    binding.source_kind == evidence.source_kind
                    or (
                        binding.source_kind == SourceKind.HISTORICAL_PRD_CORPUS
                        and evidence.source_kind
                        == SourceKind.HISTORICAL_PRD_DOCUMENT
                    )
                )
                and (
                    binding.source_id == evidence.source_id
                    or (
                        evidence.source_kind
                        == SourceKind.HISTORICAL_PRD_DOCUMENT
                        and binding.source_id == evidence.locator.get("corpus_id")
                    )
                )
            ]
            if not candidates:
                return GroundingVerdict.INVALID_SOURCE, "SOURCE_BINDING_MISMATCH"
            binding = next(
                (
                    item
                    for item in candidates
                    if item.source_version == evidence.source_version
                    or (
                        evidence.source_kind
                        == SourceKind.HISTORICAL_PRD_DOCUMENT
                        and item.source_version
                        == evidence.locator.get("corpus_version")
                    )
                ),
                None,
            )
            if binding is None:
                return GroundingVerdict.STALE_SOURCE, "SOURCE_VERSION_MISMATCH"
            if (
                evidence.access_scope_hash
                and evidence.access_scope_hash != binding.access_scope_hash
            ):
                return GroundingVerdict.INVALID_SOURCE, "ACCESS_SCOPE_MISMATCH"
            return None
        if evidence.source_kind != SourceKind.CODE_REPOSITORY:
            return GroundingVerdict.INVALID_SOURCE, "SOURCE_KIND_NOT_ALLOWED"
        if evidence.repository_id != request.repository_id:
            return GroundingVerdict.INVALID_SOURCE, "REPOSITORY_MISMATCH"
        if evidence.resolved_commit_sha != request.resolved_commit_sha:
            return GroundingVerdict.STALE_SOURCE, "COMMIT_MISMATCH"
        return None

    @staticmethod
    def _assess_claim(claim, fact_verdicts, facts_by_id):
        if claim.kind in {ClaimKind.CURRENT_STATE, ClaimKind.HISTORICAL_CONTEXT}:
            if not claim.fact_ids:
                return ClaimGroundingAssessment(
                    claim_id=claim.claim_id,
                    kind=claim.kind,
                    verdict=GroundingVerdict.UNSUPPORTED,
                    action=GroundingAction.RETRY_INVESTIGATION,
                    reason_code=(
                        "CURRENT_STATE_FACT_REQUIRED"
                        if claim.kind == ClaimKind.CURRENT_STATE
                        else "HISTORICAL_FACT_REQUIRED"
                    ),
                )
            valid = tuple(
                item
                for item in claim.fact_ids
                if fact_verdicts.get(item) == GroundingVerdict.SUPPORTED
                and GroundingService._fact_can_support_claim(
                    facts_by_id.get(item), claim.kind
                )
            )
            invalid = tuple(item for item in claim.fact_ids if item not in valid)
            verdict = (
                GroundingVerdict.SUPPORTED
                if len(valid) == len(claim.fact_ids)
                else GroundingVerdict.UNSUPPORTED
            )
            return ClaimGroundingAssessment(
                claim_id=claim.claim_id,
                kind=claim.kind,
                verdict=verdict,
                valid_fact_ids=valid,
                invalid_fact_ids=invalid,
                action=(
                    GroundingAction.PASS
                    if verdict == GroundingVerdict.SUPPORTED
                    else GroundingAction.RETRY_INVESTIGATION
                ),
                reason_code=(
                    "FACTS_SUPPORT_CLAIM"
                    if verdict == GroundingVerdict.SUPPORTED
                    else (
                        "CLAIM_HAS_UNSUPPORTED_FACT"
                        if claim.kind == ClaimKind.CURRENT_STATE
                        else "HISTORICAL_CLAIM_HAS_UNSUPPORTED_FACT"
                    )
                ),
            )
        if claim.kind == ClaimKind.TARGET_DECISION:
            supported = bool(claim.decision_ids)
            reason = "TARGET_DECISION_CONFIRMED" if supported else "DECISION_REQUIRED"
        elif claim.kind == ClaimKind.ASSUMPTION:
            supported = bool(claim.assumption_ids)
            reason = "ASSUMPTION_AUTHORIZED" if supported else "ASSUMPTION_AUTHORIZATION_REQUIRED"
        else:
            supported = True
            reason = "NON_DETERMINISTIC_CLAIM_LABELED"
        return ClaimGroundingAssessment(
            claim_id=claim.claim_id,
            kind=claim.kind,
            verdict=(
                GroundingVerdict.SUPPORTED
                if supported
                else GroundingVerdict.UNSUPPORTED
            ),
            action=(
                GroundingAction.PASS
                if supported
                else GroundingAction.HUMAN_CONFIRMATION_REQUIRED
            ),
            reason_code=reason,
        )

    @staticmethod
    def _fact_can_support_claim(fact, claim_kind):
        if fact is None:
            return False
        if claim_kind == ClaimKind.CURRENT_STATE:
            return (
                fact.fact_type == FactType.CODE_VERIFIED
                and fact.fact_scope == FactScope.CURRENT_STATE
            )
        if claim_kind == ClaimKind.HISTORICAL_CONTEXT:
            return (
                fact.fact_type == FactType.DOCUMENT_SUPPORTED
                and fact.fact_scope == FactScope.HISTORICAL_CONTEXT
            )
        return False
