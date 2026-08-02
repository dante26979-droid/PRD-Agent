from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from agent.information_need import NeedBudgetAllocation, SourceType


@dataclass(frozen=True)
class SupplementNeedPlan:
    schema_version: str
    plan_id: str
    context_hash: str
    parent_plan_id: str
    grounding_report_fingerprint: str
    claim_ids: tuple[str, ...]
    question: str
    required_coverage: tuple[str, ...]
    source_types: tuple[SourceType, ...]
    budget_allocation: NeedBudgetAllocation


class SupplementNeedFactory:
    policy_version = "supplement-need-policy.v1"

    def build(
        self,
        *,
        parent_plan_id: str,
        grounding_report_fingerprint: str,
        claim_ids: tuple[str, ...],
        question: str,
        requested_coverage: tuple[str, ...],
        parent_coverage: tuple[str, ...],
        requested_source_types: tuple[SourceType, ...],
        parent_source_types: tuple[SourceType, ...],
        remaining_budget: NeedBudgetAllocation,
    ) -> SupplementNeedPlan:
        claims = tuple(sorted(set(claim_ids)))
        coverage = tuple(dict.fromkeys(requested_coverage))
        sources = tuple(dict.fromkeys(requested_source_types))
        if not parent_plan_id or not claims or not question.strip() or not coverage:
            raise ValueError("Supplement Need is incomplete")
        if not set(coverage).issubset(parent_coverage):
            raise ValueError("Supplement Need cannot expand parent coverage")
        if not set(sources).issubset(parent_source_types):
            raise ValueError("Supplement Need cannot expand parent sources")
        identity = {
            "parent_plan_id": parent_plan_id,
            "grounding_report_fingerprint": grounding_report_fingerprint,
            "claim_ids": claims,
            "question": question.strip(),
            "required_coverage": coverage,
            "source_types": [item.value for item in sources],
            "budget": {
                "max_model_attempts": remaining_budget.max_model_attempts,
                "max_tool_calls": min(remaining_budget.max_tool_calls, 1),
                "max_iterations": min(remaining_budget.max_iterations, 1),
                "max_replans": 0,
            },
            "policy_version": self.policy_version,
        }
        context_hash = _hash(identity)
        return SupplementNeedPlan(
            schema_version="supplement-need-plan.v1",
            plan_id="need-supplement-" + context_hash.removeprefix("sha256:")[:20],
            context_hash=context_hash,
            parent_plan_id=parent_plan_id,
            grounding_report_fingerprint=grounding_report_fingerprint,
            claim_ids=claims,
            question=question.strip(),
            required_coverage=coverage,
            source_types=sources,
            budget_allocation=NeedBudgetAllocation(
                max_model_attempts=remaining_budget.max_model_attempts,
                max_tool_calls=min(remaining_budget.max_tool_calls, 1),
                max_iterations=min(remaining_budget.max_iterations, 1),
                max_replans=0,
            ),
        )


def _hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
