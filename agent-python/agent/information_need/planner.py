from __future__ import annotations

from dataclasses import replace
import json
from typing import Mapping

from agent.runtime import BudgetVector, LedgerCallSpec, RunExecutionLedger, RuntimeEventSink
from agent.runtime.idempotency import canonical_json, request_hash

from .artifact import (
    build_plan_artifact_for_run,
    decode_plan_artifact,
    plan_artifact_key,
)
from .models import NeedPlanningContext, PlannedNeedDecision, PlannedNeedDraft
from .policies import InformationNeedPolicy, planning_context_hash
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, planning_payload


class InformationNeedPlanner:
    def __init__(
        self,
        model: object,
        ledger: RunExecutionLedger,
        sink: RuntimeEventSink,
        policy: InformationNeedPolicy | None = None,
        resume_artifacts: tuple[object, ...] = (),
    ) -> None:
        self._model = model
        self._ledger = ledger
        self._sink = sink
        self._policy = policy or InformationNeedPolicy()
        self._resume_artifacts = tuple(resume_artifacts)

    def plan(self, context: NeedPlanningContext) -> PlannedNeedDecision:
        payload = planning_payload(context)
        plan_request_hash = request_hash(
            {
                "planning_payload": payload,
                "prompt_version": PROMPT_VERSION,
                "policy_version": self._policy.policy_version,
            }
        )
        existing = next(
            (
                item
                for item in self._resume_artifacts
                if getattr(item, "artifact_key", "") == plan_artifact_key(context.run_id)
            ),
            None,
        )
        if existing is not None:
            if existing.request_hash != plan_request_hash:
                raise ValueError("NEED_CONTEXT_MISMATCH")
            plan = decode_plan_artifact(existing)
            if plan.context_hash != planning_context_hash(context):
                raise ValueError("NEED_CONTEXT_MISMATCH")
            return PlannedNeedDecision(
                plan=plan,
                route=plan.route,
                reason_code=plan.route_reason_code,
                replayed=True,
                artifact_key=existing.artifact_key,
                artifact_hash=existing.content_hash,
                artifact_request_hash=existing.request_hash,
            )

        request_json = canonical_json(payload).decode("utf-8")
        spec = LedgerCallSpec(
            entry_kind="MODEL",
            operation="plan_information_need",
            operation_key="model:plan_information_need:plan1",
            request_hash=plan_request_hash,
            reservation=BudgetVector(
                model_attempts=1,
                input_tokens=max(1, len(request_json.encode("utf-8")) // 4),
                output_tokens=min(1024, max(self._ledger.remaining().output_tokens, 0)),
            ),
            outcome_schema="information-need-draft.v1",
        )

        def invoke() -> dict[str, object]:
            response = self._model.complete(SYSTEM_PROMPT, request_json)
            try:
                raw = json.loads(response.output)
            except (TypeError, json.JSONDecodeError) as error:
                raise ValueError("PLANNER_OUTPUT_INVALID") from error
            draft = PlannedNeedDraft.from_mapping(raw)
            return {
                "draft": draft.as_dict(),
                "token_usage": dict(response.token_usage),
                "model_id": response.model_id,
            }

        outcome = self._ledger.execute_model(
            spec,
            invoke,
            _validate_outcome,
            consumption=lambda value: BudgetVector(
                model_attempts=1,
                input_tokens=spec.reservation.input_tokens,
                output_tokens=_total_tokens(value["token_usage"]),
            ),
        )
        draft = PlannedNeedDraft.from_mapping(outcome.value["draft"])
        remaining = self._ledger.remaining()
        effective_context = replace(
            context,
            remaining_model_attempts=remaining.model_attempts,
            remaining_tool_calls=remaining.tool_calls,
            remaining_iterations=remaining.iterations,
            remaining_replans=remaining.replans,
        )
        decision = self._policy.apply(effective_context, draft)
        artifact = build_plan_artifact_for_run(
            context.run_id, decision.plan, request_hash=plan_request_hash
        )
        self._sink.artifact(artifact)
        return replace(
            decision,
            replayed=outcome.replayed,
            artifact_key=artifact.artifact_key,
            artifact_hash=artifact.content_hash,
            artifact_request_hash=artifact.request_hash,
        )


def _validate_outcome(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "draft",
        "token_usage",
        "model_id",
    }:
        raise ValueError("PLANNER_OUTPUT_INVALID")
    draft = PlannedNeedDraft.from_mapping(value["draft"])
    usage = value["token_usage"]
    if not isinstance(usage, Mapping) or any(
        not isinstance(item, int) or item < 0 for item in usage.values()
    ):
        raise ValueError("PLANNER_OUTPUT_INVALID")
    return {
        "draft": draft.as_dict(),
        "token_usage": dict(usage),
        "model_id": str(value["model_id"]),
    }


def _total_tokens(value: object) -> int:
    if not isinstance(value, Mapping):
        return 0
    total = value.get("total_tokens") or value.get("total")
    if isinstance(total, int):
        return total
    return sum(item for item in value.values() if isinstance(item, int))
