from __future__ import annotations

from dataclasses import replace
import json

from agent.context_pack import ContextPolicy
from agent.model_execution import ModelCallIntent, ModelExecutionModule
from agent.runtime import RunExecutionLedger, RuntimeEventSink
from agent.runtime.idempotency import request_hash

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
        context_policy: ContextPolicy = ContextPolicy(),
        resume_artifacts: tuple[object, ...] = (),
    ) -> None:
        self._model = model
        self._ledger = ledger
        self._sink = sink
        self._policy = policy or InformationNeedPolicy()
        self._context_policy = context_policy
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

        execution = ModelExecutionModule(
            self._model,
            context_policy=self._context_policy,
        ).execute(
            context=self._ledger.context,
            sink=self._sink,
            ledger=self._ledger,
            intent=ModelCallIntent(
                operation="plan_information_need",
                operation_sequence=1,
                operation_key="model:plan_information_need:plan1",
                prompt_version=PROMPT_VERSION,
                system_prompt=SYSTEM_PROMPT,
                max_output_tokens=1024,
                output_schema="information-need-plan-draft.v1",
                base_request_hash=plan_request_hash,
            ),
            payload=payload,
        )
        try:
            raw = json.loads(execution.response.output)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("PLANNER_OUTPUT_INVALID") from error
        draft = PlannedNeedDraft.from_mapping(raw)
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
            replayed=execution.replayed,
            artifact_key=artifact.artifact_key,
            artifact_hash=artifact.content_hash,
            artifact_request_hash=artifact.request_hash,
        )
