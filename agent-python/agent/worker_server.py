from __future__ import annotations

import inspect
import threading
from datetime import datetime, timezone
from typing import Callable

import grpc

from agent.v1 import agent_worker_pb2 as worker
from agent.v1 import agent_worker_pb2_grpc as worker_rpc

from .cancellation import AgentCancelled, CancellationToken
from .capability import CapabilityError
from .checkpoint import CheckpointError
from .context import Lease, RunContext
from .model import ModelApiError
from .result import AgentResult


CONTRACT_VERSION = "agent-execution.v1"


class InvalidAgentResult(ValueError):
    pass


class AgentWorkerServer(worker_rpc.AgentWorkerServiceServicer):
    """Private Go -> Python execution adapter.

    The adapter owns no durable state. It runs the Python AgentLoop and emits
    progress events; the Go caller validates and persists those events.
    """

    def __init__(
        self,
        loop: Callable[..., AgentResult],
        *,
        worker_id: str,
        max_inflight: int = 1,
        model_ready: bool = True,
        capability_ready: bool = False,
        max_checkpoint_bytes: int = 256 * 1024,
        max_draft_bytes: int = 1024 * 1024,
        max_evidence_items: int = 100,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id is required")
        if max_inflight < 1:
            raise ValueError("max_inflight must be positive")
        if min(max_checkpoint_bytes, max_draft_bytes, max_evidence_items) < 1:
            raise ValueError("Agent result limits must be positive")
        self._loop = loop
        self._worker_id = worker_id
        self._max_inflight = max_inflight
        self._model_ready = model_ready
        self._capability_ready = capability_ready
        self._max_checkpoint_bytes = max_checkpoint_bytes
        self._max_draft_bytes = max_draft_bytes
        self._max_evidence_items = max_evidence_items
        self._slots = threading.BoundedSemaphore(max_inflight)
        self._active: dict[str, CancellationToken] = {}
        self._active_lock = threading.Lock()

    def ExecuteRun(self, request, context):  # noqa: N802 - generated RPC name
        if not self._valid_meta(request.meta) or not request.dispatch_id or not request.run_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "contract metadata, dispatch_id and run_id are required")
        if request.worker_id != self._worker_id:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "worker identity mismatch")
        if request.lease.run_id != request.run_id or request.lease.worker_id != self._worker_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "lease does not match execution request")
        if not self._slots.acquire(blocking=False):
            context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "worker is at capacity")

        cancel_event = CancellationToken()
        with self._active_lock:
            self._active[request.dispatch_id] = cancel_event
        try:
            yield self._event(request, 1, "RUN_STARTED")
            run_context = RunContext(
                run_id=request.input.run_id,
                tenant_id=request.input.tenant_id,
                owner_id=request.input.owner_id,
                task_id=request.input.task_id,
                task_message=request.input.task_message,
                workflow_version=request.input.workflow_version,
                checkpoint=request.input.checkpoint,
                checkpoint_sequence=request.input.checkpoint_sequence,
                task_version=request.input.task_version or 1,
                dispatch_id=request.dispatch_id,
                lease=Lease(
                    run_id=request.lease.run_id,
                    lease_id=request.lease.lease_id,
                    worker_id=request.lease.worker_id,
                    fencing_token=request.lease.fencing_token,
                    expires_at=request.lease.expires_at,
                ),
                repository_binding_id=request.input.repository_binding_id,
                repository_revision=request.input.repository_revision,
            )
            try:
                result = self._invoke_loop(run_context, cancel_event)
                if cancel_event.is_cancelled() or not context.is_active():
                    yield self._event(
                        request,
                        2,
                        "RUN_FAILED",
                        error_category="CANCELLED",
                        retryable=False,
                    )
                    return
                self._validate_result(result, request.input.checkpoint_sequence)
                sequence = 2
                for attempt in (result.attempt, *result.additional_attempts):
                    yield self._event(request, sequence, "MODEL_ATTEMPT", model_attempt=attempt)
                    sequence += 1
                if result.evidence:
                    yield self._event(request, sequence, "EVIDENCE_APPENDED", evidence_items=result.evidence)
                    sequence += 1
                if result.checkpoint_sequence is not None:
                    yield self._event(
                        request,
                        sequence,
                        "CHECKPOINT_SAVED",
                        checkpoint_sequence=result.checkpoint_sequence,
                        checkpoint=result.checkpoint,
                    )
                    sequence += 1
                if result.draft_key is not None and result.expected_task_version is not None:
                    yield self._event(
                        request,
                        sequence,
                        "DRAFT_SUBMITTED",
                        draft_key=result.draft_key,
                        expected_task_version=result.expected_task_version,
                        draft_patch=result.draft_patch,
                    )
                    sequence += 1
                yield self._event(request, sequence, "RUN_COMPLETED", result_type="SUCCEEDED")
            except Exception as error:  # the Go side owns durable failure state
                category, retryable = self._error_details(error)
                yield self._event(
                    request,
                    2,
                    "RUN_FAILED",
                    error_category=category,
                    retryable=retryable,
                )
        finally:
            with self._active_lock:
                self._active.pop(request.dispatch_id, None)
            self._slots.release()

    def CancelRun(self, request, context):  # noqa: N802 - generated RPC name
        if not self._valid_meta(request.meta) or not request.dispatch_id or not request.run_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "contract metadata, dispatch_id and run_id are required")
        if request.worker_id != self._worker_id:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "worker identity mismatch")
        with self._active_lock:
            event = self._active.get(request.dispatch_id)
        if event is None:
            return worker.CancelRunResponse(accepted=False)
        event.cancel()
        return worker.CancelRunResponse(accepted=True)

    def Health(self, request, context):  # noqa: N802 - generated RPC name
        if not self._valid_meta(request.meta):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "contract metadata is required")
        if request.worker_id and request.worker_id != self._worker_id:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "worker identity mismatch")
        with self._active_lock:
            active = len(self._active)
        return worker.HealthResponse(
            worker_id=self._worker_id,
            status="READY",
            active_runs=active,
            max_inflight=self._max_inflight,
            model_ready=self._model_ready,
            capability_ready=self._capability_ready,
        )

    @staticmethod
    def _valid_meta(meta) -> bool:
        return bool(
            meta
            and meta.contract_version == CONTRACT_VERSION
            and meta.request_id
            and meta.correlation_id
        )

    def _invoke_loop(self, run_context: RunContext, cancel_event: CancellationToken) -> AgentResult:
        parameters = inspect.signature(self._loop).parameters.values()
        accepts_cancel = len(parameters) >= 2 or any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters
        )
        if accepts_cancel:
            return self._loop(run_context, cancel_event)
        return self._loop(run_context)

    def _validate_result(self, result: AgentResult, current_checkpoint_sequence: int) -> None:
        if not isinstance(result, AgentResult):
            raise InvalidAgentResult("Agent Loop must return AgentResult")
        for attempt in (result.attempt, *result.additional_attempts):
            if (
                not attempt.attempt_key
                or not attempt.operation
                or not attempt.request_hash
                or not attempt.status
            ):
                raise InvalidAgentResult("Model Attempt metadata is incomplete")
        if len(result.evidence) > self._max_evidence_items:
            raise InvalidAgentResult("Evidence count exceeds limit")
        if len(result.checkpoint) > self._max_checkpoint_bytes:
            raise InvalidAgentResult("Checkpoint exceeds size limit")
        if result.checkpoint_sequence is not None:
            if result.checkpoint_sequence <= current_checkpoint_sequence or not result.checkpoint:
                raise InvalidAgentResult("Checkpoint sequence or payload is invalid")
        if len(result.draft_patch) > self._max_draft_bytes:
            raise InvalidAgentResult("Draft patch exceeds size limit")
        has_draft = bool(result.draft_key or result.draft_patch)
        if has_draft and (
            not result.draft_key
            or result.expected_task_version is None
            or result.expected_task_version < 1
            or not result.draft_patch
        ):
            raise InvalidAgentResult("Draft result is incomplete")

    @staticmethod
    def _error_details(error: Exception) -> tuple[str, bool]:
        if isinstance(error, AgentCancelled):
            return "CANCELLED", False
        if isinstance(error, InvalidAgentResult):
            return "INVALID_AGENT_RESULT", False
        if isinstance(error, CheckpointError):
            return "CHECKPOINT_INCOMPATIBLE", False
        if isinstance(error, ModelApiError):
            return "MODEL_" + error.code.upper(), error.retryable
        if isinstance(error, CapabilityError):
            return "CAPABILITY_" + error.code.upper(), error.retryable
        return type(error).__name__.upper(), True

    def _event(self, request, sequence: int, event_type: str, **kwargs):
        result = worker.ExecuteRunResponse(
            dispatch_id=request.dispatch_id,
            run_id=request.run_id,
            event_sequence=sequence,
            event_id=f"{request.dispatch_id}-{sequence}",
            event_type=event_type,
            occurred_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            contract_version=CONTRACT_VERSION,
            correlation_id=request.meta.correlation_id,
        )
        attempt = kwargs.get("model_attempt")
        if attempt is not None:
            result.model_attempt.CopyFrom(
                worker.ModelAttemptEvent(
                    attempt_key=attempt.attempt_key,
                    operation=attempt.operation,
                    prompt_version=attempt.prompt_version,
                    provider=attempt.provider,
                    request_hash=attempt.request_hash,
                    status=attempt.status,
                    response_metadata_json=attempt.response_metadata_json,
                    token_usage_json=attempt.token_usage_json,
                    error_category=attempt.error_category,
                )
            )
        evidence = kwargs.get("evidence_items")
        if evidence:
            result.evidence_items.extend(evidence)
        if kwargs.get("checkpoint_sequence") is not None:
            result.checkpoint_sequence = kwargs["checkpoint_sequence"]
            result.checkpoint = kwargs.get("checkpoint", b"")
        if kwargs.get("draft_key") is not None:
            result.draft_key = kwargs["draft_key"]
            result.expected_task_version = kwargs["expected_task_version"]
            result.draft_patch = kwargs.get("draft_patch", b"")
        if kwargs.get("error_category") is not None:
            result.error_category = kwargs["error_category"]
            result.retryable = kwargs.get("retryable", False)
        if kwargs.get("result_type") is not None:
            result.result_type = kwargs["result_type"]
        return result
