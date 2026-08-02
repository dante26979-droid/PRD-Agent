from __future__ import annotations

import re

from agent.draft import ClaimCriticality, ClaimType, DraftBundle
from agent.knowledge import (
    FactScope,
    FactType,
    KnowledgeBundle,
    VerificationStatus,
    VerifiedFact,
)

from .models import GroundingFinding, GroundingOutcome, GroundingStatus


_REQUIRED = frozenset({ClaimType.CURRENT_STATE, ClaimType.CONSTRAINT})


class GroundingModule:
    """Assess claims through validated facts rather than reference presence."""

    def assess(
        self,
        bundle: DraftBundle,
        knowledge: KnowledgeBundle,
        *,
        supplement_count: int,
        max_supplements: int,
        has_remaining_tool_budget: bool,
    ) -> tuple[tuple[GroundingFinding, ...], GroundingOutcome]:
        evidence_by_id = {item.evidence_id: item for item in knowledge.evidence}
        conflict_fact_ids = {
            fact_id
            for conflict in knowledge.conflicts
            if conflict.status == "OPEN"
            for fact_id in conflict.fact_ids
        }
        findings: list[GroundingFinding] = []
        for claim in bundle.claims:
            if claim.claim_type not in _REQUIRED:
                findings.append(
                    GroundingFinding(
                        claim.claim_id,
                        GroundingStatus.NOT_REQUIRED,
                        (),
                        "CLAIM_TYPE_NOT_GROUNDING_REQUIRED",
                    )
                )
                continue
            related = tuple(
                fact
                for fact in knowledge.facts
                if _fact_mentions_claim(fact, claim.statement, evidence_by_id)
            )
            conflicting = tuple(
                fact for fact in related if fact.fact_id in conflict_fact_ids
            )
            if conflicting:
                findings.append(
                    _finding(
                        claim.claim_id,
                        GroundingStatus.CONFLICTING,
                        conflicting,
                        "OPEN_SOURCE_CONFLICT",
                    )
                )
                continue
            scoped = tuple(
                fact
                for fact in related
                if _scope_supports(fact, claim.claim_type)
                and fact.verification_status is VerificationStatus.SUPPORTED
            )
            if scoped:
                findings.append(
                    _finding(
                        claim.claim_id,
                        GroundingStatus.SUPPORTED,
                        scoped,
                        "VERIFIED_FACT_SUPPORT",
                    )
                )
            elif related:
                reason = (
                    "FACT_TYPE_MISMATCH"
                    if any(
                        _scope_matches(fact, claim.claim_type) for fact in related
                    )
                    else "FACT_SCOPE_MISMATCH"
                )
                findings.append(
                    _finding(
                        claim.claim_id,
                        GroundingStatus.UNSUPPORTED,
                        related,
                        reason,
                    )
                )
            else:
                findings.append(
                    GroundingFinding(
                        claim.claim_id,
                        GroundingStatus.UNSUPPORTED,
                        (),
                        "FACT_REQUIRED",
                    )
                )

        blocking = any(
            finding.status
            in {
                GroundingStatus.UNSUPPORTED,
                GroundingStatus.PARTIAL,
                GroundingStatus.CONFLICTING,
            }
            and next(
                claim for claim in bundle.claims if claim.claim_id == finding.claim_id
            ).criticality
            is not ClaimCriticality.INFORMATIONAL
            for finding in findings
        )
        if not blocking:
            outcome = GroundingOutcome.GROUNDED
        elif supplement_count < max_supplements and has_remaining_tool_budget:
            outcome = GroundingOutcome.SUPPLEMENT_REQUIRED
        else:
            outcome = GroundingOutcome.PARTIAL
        return tuple(findings), outcome


def _finding(
    claim_id: str,
    status: GroundingStatus,
    facts: tuple[VerifiedFact, ...],
    reason: str,
) -> GroundingFinding:
    return GroundingFinding(
        claim_id=claim_id,
        status=status,
        evidence_refs=tuple(
            sorted({value for fact in facts for value in fact.evidence_ids})
        ),
        reason_code=reason,
        fact_ids=tuple(sorted(fact.fact_id for fact in facts)),
    )


def _scope_supports(fact: VerifiedFact, claim_type: ClaimType) -> bool:
    if not _scope_matches(fact, claim_type):
        return False
    if claim_type is ClaimType.CURRENT_STATE:
        return fact.fact_type in {
            FactType.CODE_VERIFIED,
            FactType.USER_CONFIRMED,
        }
    if claim_type is ClaimType.CONSTRAINT:
        return fact.fact_type in {
            FactType.CODE_VERIFIED,
            FactType.USER_CONFIRMED,
        }
    return False


def _scope_matches(fact: VerifiedFact, claim_type: ClaimType) -> bool:
    if claim_type is ClaimType.CURRENT_STATE:
        return fact.fact_scope is FactScope.CURRENT_STATE
    if claim_type is ClaimType.CONSTRAINT:
        return fact.fact_scope in {
            FactScope.CURRENT_STATE,
            FactScope.TARGET_DECISION,
        }
    return False


def _fact_mentions_claim(
    fact: VerifiedFact,
    statement: str,
    evidence_by_id: dict[str, object],
) -> bool:
    tokens = tuple(token for token in _tokens(statement) if len(token) > 1)
    if not tokens:
        return False
    searchable = " ".join(
        (
            fact.subject,
            fact.predicate,
            str(fact.value),
            *(str(getattr(evidence_by_id.get(item), "excerpt", "")) for item in fact.evidence_ids),
        )
    )
    available = _tokens(searchable)
    return all(token in available for token in tokens)


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        re.findall(r"[a-z0-9\u4e00-\u9fff]+", value.lower().replace("_", " "))
    )
