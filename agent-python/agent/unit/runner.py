from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Mapping

from agent.quality.full_review import FullDocumentQualityPolicy, FullReviewRequest
from agent.quality.models import QualityOutcome
from agent.quality.policies import DocumentQualityPolicy, UnitQualityRequest
from agent.unit.invariants import UnitInvariantPolicy
from agent.unit.models import (
    OutlineCandidate,
    RunOutputKind,
    RunPurpose,
    UnitCandidate,
    UnitPatch,
    UnitRunRequest,
    UnitRunResult,
)


@dataclass(frozen=True)
class ReviewableUnitModule:
    outline_generator: Callable[[UnitRunRequest], OutlineCandidate | Mapping[str, object]] | None = None
    unit_generator: Callable[[UnitRunRequest], UnitCandidate | Mapping[str, object]] | None = None
    unit_reviser: Callable[[UnitRunRequest], UnitPatch | Mapping[str, object]] | None = None
    grounding_evaluator: Callable[[UnitCandidate], tuple[Mapping[str, object], ...]] | None = None
    quality_policy: DocumentQualityPolicy = DocumentQualityPolicy()
    full_review_policy: FullDocumentQualityPolicy = FullDocumentQualityPolicy()
    invariants: UnitInvariantPolicy = UnitInvariantPolicy()

    def run(self, request: UnitRunRequest) -> UnitRunResult:
        if request.purpose is RunPurpose.PLAN_OUTLINE:
            if self.outline_generator is None:
                raise ValueError("outline generator is unavailable")
            raw = self.outline_generator(request)
            candidate = raw if isinstance(raw, OutlineCandidate) else OutlineCandidate.from_dict(raw)
            return self._result(request, RunOutputKind.OUTLINE_CANDIDATE, candidate.as_dict())
        if request.purpose is RunPurpose.GENERATE_UNIT:
            if self.unit_generator is None:
                raise ValueError("unit generator is unavailable")
            raw = self.unit_generator(request)
            candidate = raw if isinstance(raw, UnitCandidate) else UnitCandidate.from_dict(raw)
            self.invariants.validate_candidate(request, candidate)
            grounding_findings = (
                self.grounding_evaluator(candidate)
                if self.grounding_evaluator is not None
                else request.grounding_findings
            )
            report = self.quality_policy.evaluate_unit(
                UnitQualityRequest(
                    candidate=candidate,
                    required_content=self._required_content(request),
                    grounding_findings=grounding_findings,
                    unresolved_conflict_ids=request.unresolved_conflict_ids,
                    unresolved_unknown_ids=request.unresolved_unknown_ids,
                )
            )
            generations = self._next_generations(request)
            payload = {**candidate.as_dict(), "quality_report": report.as_dict(), **generations}
            return self._result(request, RunOutputKind.UNIT_CANDIDATE, payload)
        if request.purpose is RunPurpose.REVISE_UNIT:
            if self.unit_reviser is None:
                raise ValueError("unit reviser is unavailable")
            raw = self.unit_reviser(request)
            patch = raw if isinstance(raw, UnitPatch) else UnitPatch.from_dict(raw)
            self.invariants.validate_patch(request, patch)
            base = request.base_candidate
            assert base is not None
            revised = UnitCandidate(
                unit_key=base.unit_key,
                title=base.title,
                ordinal=base.ordinal,
                node_keys=base.node_keys,
                markdown=patch.replacement_markdown,
                claims=patch.claims,
                unknown_ids=patch.preserved_unknown_ids,
                used_fact_ids=patch.used_fact_ids,
            )
            grounding_findings = (
                self.grounding_evaluator(revised)
                if self.grounding_evaluator is not None
                else request.grounding_findings
            )
            report = self.quality_policy.evaluate_unit(
                UnitQualityRequest(
                    candidate=revised,
                    required_content=self._required_content(request),
                    grounding_findings=grounding_findings,
                    unresolved_conflict_ids=request.unresolved_conflict_ids,
                    unresolved_unknown_ids=request.unresolved_unknown_ids,
                )
            )
            payload = {
                **patch.as_dict(),
                "quality_report": report.as_dict(),
                "requires_regrounding": self.grounding_evaluator is None,
                **self._next_generations(request),
            }
            return self._result(request, RunOutputKind.UNIT_PATCH, payload)
        if request.purpose is RunPurpose.FULL_REVIEW:
            outline = self._outline_from_scope(request)
            report = self.full_review_policy.evaluate(
                FullReviewRequest(outline=outline, units=request.scope.confirmed_context)
            )
            payload = {
                "schema_version": "full-review-report.v1",
                "outline_hash": request.scope.outline_hash.removeprefix("sha256:"),
                "outcome": "PASSED" if report.outcome is QualityOutcome.PASSED else "NEEDS_REVISION",
                "issues": [item.as_dict() for item in report.issues],
                "unit_hashes": dict(report.unit_hashes),
            }
            return self._result(request, RunOutputKind.FULL_REVIEW_REPORT, payload)
        raise ValueError(f"unsupported run purpose: {request.purpose}")

    @staticmethod
    def _result(request: UnitRunRequest, kind: RunOutputKind, payload: Mapping[str, object]) -> UnitRunResult:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return UnitRunResult(
            purpose=request.purpose,
            output_kind=kind,
            scope_hash=request.scope.scope_hash,
            content_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            payload=payload,
        )

    @staticmethod
    def _required_content(request: UnitRunRequest) -> tuple[str, ...]:
        if request.required_content:
            return request.required_content
        value = request.scope.requirement_brief_ref
        if value.startswith("required:"):
            return tuple(item for item in value.removeprefix("required:").split(",") if item)
        return ()

    @staticmethod
    def _outline_from_scope(request: UnitRunRequest) -> OutlineCandidate:
        if request.locked_outline is not None:
            return request.locked_outline
        raw = request.scope.requirement_brief_ref
        if not raw.startswith("outline-json:"):
            raise ValueError("full review requires the locked outline payload")
        value = json.loads(raw.removeprefix("outline-json:"))
        return OutlineCandidate.from_dict(value)

    @staticmethod
    def _next_generations(request: UnitRunRequest) -> dict[str, int]:
        resume = request.resume
        return {
            "claim_generation": (resume.claim_generation if resume else 0) + 1,
            "grounding_generation": (resume.grounding_generation if resume else 0) + 1,
            "quality_generation": (resume.quality_generation if resume else 0) + 1,
        }
