from __future__ import annotations

import hashlib
from typing import Mapping

from agent.runtime.outcomes import build_artifact, decode_outcome
from agent.v1 import agent_execution_pb2 as proto

from .models import (
    CoverageRequirement,
    InformationNeedPlan,
    NeedBudgetAllocation,
    NeedKind,
    NeedRoute,
    Requiredness,
    SourceType,
)


PLAN_SCHEMA = "information-need-plan.v1"
PLAN_ARTIFACT_TYPE = "INFORMATION_NEED_PLAN"


def plan_artifact_key(run_id: str) -> str:
    return f"{run_id}:information_need:plan:1"


def build_plan_artifact_for_run(
    run_id: str, plan: InformationNeedPlan, *, request_hash: str
) -> proto.RunArtifact:
    return build_artifact(
        key=plan_artifact_key(run_id),
        artifact_type=PLAN_ARTIFACT_TYPE,
        request_hash=request_hash,
        schema=PLAN_SCHEMA,
        value=plan_as_dict(plan),
    )


def decode_plan_artifact(artifact: proto.RunArtifact) -> InformationNeedPlan:
    if artifact.artifact_type != PLAN_ARTIFACT_TYPE:
        raise ValueError("information need artifact type mismatch")
    if artifact.generation != 1:
        raise ValueError("information need artifact generation mismatch")
    actual_hash = hashlib.sha256(artifact.content).hexdigest()
    if artifact.content_hash.removeprefix("sha256:") != actual_hash:
        raise ValueError("information need artifact content hash mismatch")
    return decode_outcome(artifact.content, PLAN_SCHEMA, plan_from_mapping)


def plan_as_dict(plan: InformationNeedPlan) -> dict[str, object]:
    return {
        "schema_version": plan.schema_version,
        "plan_id": plan.plan_id,
        "context_hash": plan.context_hash,
        "question": plan.question,
        "need_kind": plan.need_kind.value,
        "suggested_requiredness": plan.suggested_requiredness.value,
        "effective_requiredness": plan.effective_requiredness.value,
        "requiredness_reason_code": plan.requiredness_reason_code,
        "source_types": [item.value for item in plan.source_types],
        "required_coverage": [
            {
                "key": item.key,
                "source_type": item.source_type.value,
                "description": item.description,
                "blocking": item.blocking,
            }
            for item in plan.required_coverage
        ],
        "route": plan.route.value,
        "route_reason_code": plan.route_reason_code,
        "fallback": plan.fallback,
        "budget_allocation": {
            "max_model_attempts": plan.budget_allocation.max_model_attempts,
            "max_tool_calls": plan.budget_allocation.max_tool_calls,
            "max_iterations": plan.budget_allocation.max_iterations,
            "max_replans": plan.budget_allocation.max_replans,
        },
        "planner_version": plan.planner_version,
        "policy_version": plan.policy_version,
        "assumption_refs": list(plan.assumption_refs),
    }


def plan_from_mapping(value: object) -> InformationNeedPlan:
    if not isinstance(value, Mapping):
        raise ValueError("information need plan must be an object")
    expected = {
        "schema_version",
        "plan_id",
        "context_hash",
        "question",
        "need_kind",
        "suggested_requiredness",
        "effective_requiredness",
        "requiredness_reason_code",
        "source_types",
        "required_coverage",
        "route",
        "route_reason_code",
        "fallback",
        "budget_allocation",
        "planner_version",
        "policy_version",
        "assumption_refs",
    }
    if set(value) != expected:
        raise ValueError("information need plan fields are invalid")
    raw_coverage = value["required_coverage"]
    raw_budget = value["budget_allocation"]
    raw_sources = value["source_types"]
    raw_assumptions = value["assumption_refs"]
    if (
        not isinstance(raw_coverage, list)
        or not isinstance(raw_budget, Mapping)
        or not isinstance(raw_sources, list)
        or not isinstance(raw_assumptions, list)
    ):
        raise ValueError("information need plan nested fields are invalid")
    if set(raw_budget) != {
        "max_model_attempts",
        "max_tool_calls",
        "max_iterations",
        "max_replans",
    }:
        raise ValueError("information need budget fields are invalid")
    coverage = []
    for item in raw_coverage:
        if not isinstance(item, Mapping) or set(item) != {
            "key",
            "source_type",
            "description",
            "blocking",
        }:
            raise ValueError("information need coverage fields are invalid")
        if not isinstance(item["blocking"], bool):
            raise ValueError("information need coverage blocking flag is invalid")
        coverage.append(
            CoverageRequirement(
                key=str(item["key"]),
                source_type=SourceType(str(item["source_type"])),
                description=str(item["description"]),
                blocking=bool(item["blocking"]),
            )
        )
    return InformationNeedPlan(
        schema_version=str(value["schema_version"]),
        plan_id=str(value["plan_id"]),
        context_hash=str(value["context_hash"]),
        question=str(value["question"]),
        need_kind=NeedKind(str(value["need_kind"])),
        suggested_requiredness=Requiredness(str(value["suggested_requiredness"])),
        effective_requiredness=Requiredness(str(value["effective_requiredness"])),
        requiredness_reason_code=str(value["requiredness_reason_code"]),
        source_types=tuple(SourceType(str(item)) for item in raw_sources),
        required_coverage=tuple(coverage),
        route=NeedRoute(str(value["route"])),
        route_reason_code=str(value["route_reason_code"]),
        fallback=str(value["fallback"]),
        budget_allocation=NeedBudgetAllocation(
            max_model_attempts=int(raw_budget["max_model_attempts"]),
            max_tool_calls=int(raw_budget["max_tool_calls"]),
            max_iterations=int(raw_budget["max_iterations"]),
            max_replans=int(raw_budget["max_replans"]),
        ),
        planner_version=str(value["planner_version"]),
        policy_version=str(value["policy_version"]),
        assumption_refs=tuple(str(item) for item in raw_assumptions),
    )
