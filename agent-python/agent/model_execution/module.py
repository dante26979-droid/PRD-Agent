from __future__ import annotations

import json
from typing import Callable, Mapping

from agent.context import RunContext
from agent.context_pack import (
    ContextCompactionBudgetUnavailable,
    ContextPackModule,
    ContextPolicy,
)
from agent.model import ModelResponse
from agent.project_memory import (
    PreparedProjectMemory,
    ProjectMemoryModule,
    ProjectMemoryPolicy,
)
from agent.runtime import BudgetVector, LedgerCallSpec, RunExecutionLedger
from agent.runtime.idempotency import canonical_json, request_hash
from agent.runtime.event_sink import RuntimeEventSink

from .intent import ModelCallIntent, ModelExecutionResult


class ModelExecutionModule:
    """Shared model seam for context preparation, Ledger identity and replay."""

    def __init__(
        self,
        model: object,
        *,
        context_policy: ContextPolicy = ContextPolicy(),
        cancel_check: Callable[[], None] | None = None,
    ) -> None:
        self._model = model
        self._context_module = ContextPackModule(context_policy)
        self._cancel_check = cancel_check or (lambda: None)

    def execute(
        self,
        *,
        context: RunContext,
        sink: RuntimeEventSink,
        ledger: RunExecutionLedger,
        intent: ModelCallIntent,
        payload: Mapping[str, object],
    ) -> ModelExecutionResult:
        memory_policy = getattr(sink, "_project_memory_policy", ProjectMemoryPolicy())
        previous_memory = getattr(sink, "_latest_project_memory", None)
        if (
            intent.operation == "repair_working_draft"
            and isinstance(previous_memory, PreparedProjectMemory)
            and previous_memory.bundle is not None
        ):
            effective = dict(payload)
            if memory_policy.mode == "enforce":
                effective["project_memory"] = previous_memory.bundle.as_dict()
            memory = PreparedProjectMemory(
                payload=effective,
                bundle=previous_memory.bundle,
                capability_artifact_key=previous_memory.capability_artifact_key,
                capability_artifact_hash=previous_memory.capability_artifact_hash,
                bundle_artifact_key=previous_memory.bundle_artifact_key,
                bundle_artifact_hash=previous_memory.bundle_artifact_hash,
                replayed=True,
            )
        else:
            memory = ProjectMemoryModule(memory_policy).prepare(
                context=context,
                gateway=getattr(sink, "_project_memory_gateway", None),
                ledger=ledger,
                operation=intent.operation,
                operation_sequence=intent.operation_sequence,
                payload=payload,
            )
        setattr(sink, "_latest_project_memory", memory)
        base_request_hash = intent.base_request_hash
        if (
            memory.bundle is not None
            and memory_policy.mode == "enforce"
            and base_request_hash is not None
        ):
            base_request_hash = request_hash(
                {
                    "base_request_hash": base_request_hash,
                    "memory_assignment_hash": context.memory_assignment_hash,
                    "memory_bundle_artifact_hash": memory.bundle_artifact_hash,
                }
            )
        prepared = self._context_module.prepare(
            context=context,
            ledger=ledger,
            operation=intent.operation,
            operation_sequence=intent.operation_sequence,
            prompt_version=intent.prompt_version,
            output_schema=intent.output_schema,
            system_prompt=intent.system_prompt,
            payload=memory.payload,
            semantic_compactor=(
                lambda request: self._semantic_compact(
                    context=context,
                    ledger=ledger,
                    intent=intent,
                    request=request,
                )
            ),
            base_request_hash=base_request_hash,
        )
        # Checkpoints store only this latest identity/accounting projection; the
        # Ledger-owned Context Pack artifact remains the authoritative body.
        setattr(sink, "_latest_prepared_model_context", prepared)
        remaining = ledger.remaining()
        output_reservation = min(
            max(intent.max_output_tokens, 0), max(remaining.output_tokens, 0)
        )
        spec = LedgerCallSpec(
            entry_kind="MODEL",
            operation=intent.operation,
            operation_key=intent.operation_key,
            request_hash=prepared.request_hash,
            reservation=BudgetVector(
                model_attempts=1,
                input_tokens=prepared.estimated_input_tokens,
                output_tokens=output_reservation,
            ),
            outcome_schema="model-response.v1",
        )

        def invoke() -> dict[str, object]:
            self._cancel_check()
            response = self._model.complete(
                intent.system_prompt,
                prepared.canonical_payload.decode("utf-8"),
            )
            self._cancel_check()
            return model_response_as_dict(response)

        outcome = ledger.execute_model(
            spec,
            invoke,
            validate_model_response_dict,
            consumption=model_budget_consumption,
        )
        return ModelExecutionResult(
            response=model_response_from_dict(outcome.value),
            prepared_context=prepared,
            replayed=outcome.replayed,
        )

    def _semantic_compact(
        self,
        *,
        context: RunContext,
        ledger: RunExecutionLedger,
        intent: ModelCallIntent,
        request: Mapping[str, object],
    ) -> Mapping[str, object]:
        request_json = canonical_json(request)
        remaining = ledger.remaining()
        if remaining.model_attempts < 2:
            raise ContextCompactionBudgetUnavailable(
                "CONTEXT_COMPACTION_BUDGET_UNAVAILABLE"
            )
        compactor_input_tokens = self._context_module.estimator.estimate_bytes(
            request_json
        )
        business_view = request.get("operation_view", {})
        business_input_tokens = self._context_module.estimator.estimate_request(
            intent.system_prompt,
            canonical_json(business_view),
        )
        if remaining.input_tokens < compactor_input_tokens + business_input_tokens:
            raise ContextCompactionBudgetUnavailable(
                "CONTEXT_COMPACTION_BUDGET_UNAVAILABLE"
            )
        output_tokens = min(
            self._context_module.policy.semantic_max_output_tokens,
            max(remaining.output_tokens - intent.max_output_tokens, 0),
        )
        if output_tokens <= 0:
            raise ContextCompactionBudgetUnavailable(
                "CONTEXT_COMPACTION_BUDGET_UNAVAILABLE"
            )
        spec = LedgerCallSpec(
            entry_kind="MODEL",
            operation="compact_context",
            operation_key=(
                f"model:compact_context:{_safe_segment(intent.operation)}:"
                f"sequence{intent.operation_sequence}"
            ),
            request_hash=request_hash(
                {
                    "policy_version": self._context_module.policy.version,
                    "business_operation": intent.operation,
                    "request": request,
                }
            ),
            reservation=BudgetVector(
                model_attempts=1,
                input_tokens=compactor_input_tokens,
                output_tokens=output_tokens,
            ),
            outcome_schema="context-compaction-outcome.v1",
        )
        mandatory = frozenset(str(item) for item in request.get("mandatory_keys", []))
        original = request.get("operation_view")
        if not isinstance(original, Mapping):
            raise ValueError("CONTEXT_COMPACTION_REQUEST_INVALID")

        def invoke() -> Mapping[str, object]:
            self._cancel_check()
            response = self._model.complete(
                (
                    "You compact validated PRD Agent context. Return one JSON object "
                    "with operation_view. Preserve mandatory fields exactly and never "
                    "create facts, identifiers, decisions, unknowns, conflicts, source "
                    "references, credentials, or hidden reasoning."
                ),
                request_json.decode("utf-8"),
            )
            self._cancel_check()
            try:
                raw = json.loads(response.output)
            except (TypeError, json.JSONDecodeError) as error:
                raise ValueError("CONTEXT_COMPACTION_OUTPUT_INVALID") from error
            validated = ContextPackModule.validate_semantic_view(
                raw,
                original=original,
                mandatory_keys=mandatory,
            )
            return {
                "operation_view": validated,
                "token_usage": dict(response.token_usage),
                "model_id": response.model_id,
            }

        def validate(value: object) -> dict[str, object]:
            if not isinstance(value, Mapping):
                raise ValueError("CONTEXT_COMPACTION_OUTPUT_INVALID")
            view = value.get("operation_view")
            if not isinstance(view, Mapping):
                raise ValueError("CONTEXT_COMPACTION_OUTPUT_INVALID")
            validated = ContextPackModule.validate_semantic_view(
                view,
                original=original,
                mandatory_keys=mandatory,
            )
            usage = value.get("token_usage", {})
            if not isinstance(usage, Mapping):
                raise ValueError("CONTEXT_COMPACTION_OUTPUT_INVALID")
            return {
                "operation_view": validated,
                "token_usage": dict(usage),
                "model_id": str(value.get("model_id", "unknown")),
            }

        outcome = ledger.execute_model(
            spec,
            invoke,
            validate,
            consumption=lambda value: BudgetVector(
                model_attempts=1,
                input_tokens=spec.reservation.input_tokens,
                output_tokens=_usage_output_tokens(value.get("token_usage", {})),
            ),
        )
        return {"operation_view": outcome.value["operation_view"]}


def model_response_as_dict(response: ModelResponse) -> dict[str, object]:
    return {
        "output": response.output,
        "token_usage": dict(response.token_usage),
        "model_id": response.model_id,
        "finish_reason": response.finish_reason,
        "provider_request_id": response.provider_request_id,
        "latency_ms": response.latency_ms,
    }


def validate_model_response_dict(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or not isinstance(value.get("output"), str):
        raise ValueError("validated model outcome is invalid")
    usage = value.get("token_usage", {})
    if not isinstance(usage, Mapping) or any(
        not isinstance(item, int) or item < 0 for item in usage.values()
    ):
        raise ValueError("validated model token usage is invalid")
    return {
        "output": value["output"],
        "token_usage": dict(usage),
        "model_id": str(value.get("model_id", "unknown")),
        "finish_reason": str(value.get("finish_reason", "stop")),
        "provider_request_id": value.get("provider_request_id"),
        "latency_ms": int(value.get("latency_ms", 0)),
    }


def model_response_from_dict(value: Mapping[str, object]) -> ModelResponse:
    return ModelResponse(
        output=str(value["output"]),
        token_usage=dict(value.get("token_usage", {})),
        model_id=str(value.get("model_id", "unknown")),
        finish_reason=str(value.get("finish_reason", "stop")),
        provider_request_id=(
            str(value["provider_request_id"])
            if value.get("provider_request_id") is not None
            else None
        ),
        latency_ms=int(value.get("latency_ms", 0)),
    )


def model_budget_consumption(value: Mapping[str, object]) -> BudgetVector:
    usage = value.get("token_usage", {})
    if not isinstance(usage, Mapping):
        usage = {}
    return BudgetVector(
        model_attempts=1,
        input_tokens=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        output_tokens=int(
            usage.get("output_tokens") or usage.get("completion_tokens") or 0
        ),
    )


def _usage_output_tokens(value: object) -> int:
    if not isinstance(value, Mapping):
        return 0
    return int(value.get("output_tokens") or value.get("completion_tokens") or 0)


def _safe_segment(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in "_.-" else "-" for char in value.lower()
    )[:60]
