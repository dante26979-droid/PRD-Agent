from __future__ import annotations

import hashlib
from collections.abc import Iterable

from agent.draft import (
    Claim,
    ClaimCriticality,
    ClaimType,
    DraftBundle,
    Unknown,
)

from .models import GroundingFinding, GroundingOutcome, GroundingStatus


_GROUNDING_REQUIRED = frozenset({ClaimType.CURRENT_STATE, ClaimType.CONSTRAINT})


def assess_grounding(
    bundle: DraftBundle,
    *,
    available_evidence_refs: Iterable[str],
    supplement_count: int,
    max_supplements: int,
    has_remaining_tool_budget: bool,
) -> tuple[tuple[GroundingFinding, ...], GroundingOutcome]:
    available = frozenset(available_evidence_refs)
    findings = []
    blocking_missing = False
    blocking_conflict = False
    for claim in bundle.claims:
        if claim.claim_type not in _GROUNDING_REQUIRED:
            finding = GroundingFinding(
                claim.claim_id,
                GroundingStatus.NOT_REQUIRED,
                (),
                "CLAIM_TYPE_NOT_GROUNDING_REQUIRED",
            )
        elif not claim.evidence_refs:
            finding = GroundingFinding(
                claim.claim_id,
                GroundingStatus.UNSUPPORTED,
                (),
                "EVIDENCE_MISSING",
            )
        elif not set(claim.evidence_refs).issubset(available):
            finding = GroundingFinding(
                claim.claim_id,
                GroundingStatus.UNSUPPORTED,
                claim.evidence_refs,
                "EVIDENCE_REFERENCE_INVALID",
            )
        else:
            finding = GroundingFinding(
                claim.claim_id,
                GroundingStatus.SUPPORTED,
                claim.evidence_refs,
                "EVIDENCE_REFERENCE_VALID",
            )
        findings.append(finding)
        if claim.criticality != ClaimCriticality.INFORMATIONAL:
            blocking_missing = blocking_missing or finding.status in {
                GroundingStatus.UNSUPPORTED,
                GroundingStatus.PARTIAL,
            }
            blocking_conflict = (
                blocking_conflict
                or finding.status == GroundingStatus.CONFLICTING
            )
    if not blocking_missing and not blocking_conflict:
        outcome = GroundingOutcome.GROUNDED
    elif (
        blocking_missing
        and supplement_count < max_supplements
        and has_remaining_tool_budget
    ):
        outcome = GroundingOutcome.SUPPLEMENT_REQUIRED
    else:
        outcome = GroundingOutcome.PARTIAL
    return tuple(findings), outcome


def materialize_unknowns(
    bundle: DraftBundle,
    findings: Iterable[GroundingFinding],
) -> DraftBundle:
    by_claim = {item.claim_id: item for item in findings}
    unknowns = []
    for claim in bundle.claims:
        finding = by_claim.get(claim.claim_id)
        if finding is None or finding.status not in {
            GroundingStatus.UNSUPPORTED,
            GroundingStatus.PARTIAL,
            GroundingStatus.CONFLICTING,
        }:
            continue
        unknowns.append(
            Unknown(
                unknown_id="unknown-"
                + hashlib.sha256(claim.claim_id.encode("utf-8")).hexdigest()[:20],
                unit_key=claim.unit_key,
                statement=f"当前证据不足以确认：{claim.statement}",
                reason_code=finding.reason_code,
                related_claim_ids=(claim.claim_id,),
                required_user_input="请补充证据或确认该事实。",
            )
        )
    return bundle.with_unknowns(unknowns)
