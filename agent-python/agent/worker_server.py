from __future__ import annotations

import inspect
import secrets
import threading
from dataclasses import replace
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Callable

import grpc

from agent.v1 import agent_worker_pb2 as worker
from agent.v1 import agent_execution_pb2 as execution_proto
from agent.v1 import agent_worker_pb2_grpc as worker_rpc

from .cancellation import AgentCancelled, CancellationToken
from .capability import CapabilityError
from .checkpoint import CheckpointError
from .context import Lease, RunContext
from .model import ModelApiError
from .result import AgentResult, SubmissionDisposition
from .runtime import RuntimeEventSink, ShadowArtifactModule


CONTRACT_VERSION = "agent-execution.v2"


class InvalidAgentResult(ValueError):
    pass


class EventAckError(RuntimeError):
    pass


class _RuntimeSignal:
    def __init__(self, event_type: str, payload) -> None:
        self.event_type = event_type
        self.payload = payload
        self.released = threading.Event()


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
        max_run_artifact_bytes: int = 1024 * 1024,
        max_evidence_items: int = 100,
        service_token: str | None = None,
        event_ack_timeout_seconds: float = 0,
        supported_workflow_versions: tuple[str, ...] = (
            "agent-runtime.v1",
            "agent-runtime.v4",
        ),
        supported_execution_ledger_versions: tuple[str, ...] = ("run-ledger.v1",),
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id is required")
        if max_inflight < 1:
            raise ValueError("max_inflight must be positive")
        if min(
            max_checkpoint_bytes,
            max_draft_bytes,
            max_run_artifact_bytes,
            max_evidence_items,
        ) < 1:
            raise ValueError("Agent result limits must be positive")
        if event_ack_timeout_seconds < 0:
            raise ValueError("event acknowledgement timeout cannot be negative")
        if not supported_workflow_versions:
            raise ValueError("supported workflow versions are required")
        self._loop = loop
        self._worker_id = worker_id
        self._max_inflight = max_inflight
        self._model_ready = model_ready
        self._capability_ready = capability_ready
        self._max_checkpoint_bytes = max_checkpoint_bytes
        self._max_draft_bytes = max_draft_bytes
        self._max_run_artifact_bytes = max_run_artifact_bytes
        self._max_evidence_items = max_evidence_items
        self._service_token = service_token
        self._event_ack_timeout_seconds = event_ack_timeout_seconds
        self._supported_workflow_versions = frozenset(supported_workflow_versions)
        self._supported_execution_ledger_versions = frozenset(supported_execution_ledger_versions)
        self._slots = threading.BoundedSemaphore(max_inflight)
        self._active: dict[str, CancellationToken] = {}
        self._active_lock = threading.Lock()
        self._ack_waiters: dict[str, tuple[str, str, int, threading.Event]] = {}
        self._ack_lock = threading.Lock()

    def ExecuteRun(self, request, context):  # noqa: N802 - generated RPC name
        self._authorize(context)
        if not self._valid_meta(request.meta) or not request.dispatch_id or not request.run_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "contract metadata, dispatch_id and run_id are required")
        if request.worker_id != self._worker_id:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "worker identity mismatch")
        if request.lease.run_id != request.run_id or request.lease.worker_id != self._worker_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "lease does not match execution request")
        if request.input.workflow_version not in self._supported_workflow_versions:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "workflow version is unsupported")
        if request.input.execution_ledger_version and request.input.execution_ledger_version not in self._supported_execution_ledger_versions:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "execution ledger version is unsupported")
        if bool(request.input.execution_ledger_version) != request.input.HasField("run_budget"):
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "ledger run budget contract is incomplete")
        if not self._slots.acquire(blocking=False):
            context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "worker is at capacity")

        cancel_event = CancellationToken()
        with self._active_lock:
            self._active[request.dispatch_id] = cancel_event
        try:
            yield from self._yield_acked(
                self._event(request, 1, "RUN_STARTED"),
                context,
            )
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
                resume_evidence=tuple(request.input.resume_evidence),
                resume_artifacts=tuple(request.input.resume_artifacts),
                revision_scope=(
                    request.input.revision_scope
                    if request.input.HasField("revision_scope")
                    else None
                ),
                resume_draft=(
                    request.input.resume_draft
                    if request.input.HasField("resume_draft")
                    else None
                ),
                base_draft=(
                    request.input.base_draft
                    if request.input.HasField("base_draft")
                    else None
                ),
                submitted_draft=(
                    request.input.submitted_draft
                    if request.input.HasField("submitted_draft")
                    else None
                ),
                resume_summary=(
                    request.input.resume_summary
                    if request.input.HasField("resume_summary")
                    else None
                ),
                execution_ledger_version=request.input.execution_ledger_version,
                run_budget=(request.input.run_budget if request.input.HasField("run_budget") else None),
                consumed_budget=(request.input.consumed_budget if request.input.HasField("consumed_budget") else None),
                ledger_entries=tuple(request.input.ledger_entries),
                allowed_source_authorities=tuple(
                    request.input.allowed_source_authorities
                ),
                run_purpose=request.input.run_purpose,
                unit_scope=(
                    request.input.unit_scope
                    if request.input.HasField("unit_scope")
                    else None
                ),
                evaluation_mode=request.input.evaluation_mode,
                authoritative_workflow_version=(
                    request.input.authoritative_workflow_version
                ),
                shadow_workflow_version=request.input.shadow_workflow_version,
                candidate_policy_version=request.input.candidate_policy_version,
                assignment_hash=request.input.assignment_hash,
            )
            try:
                sequence = 2
                execution = self._invoke_loop_with_events(run_context, cancel_event)
                latest_checkpoint_sequence = request.input.checkpoint_sequence
                while True:
                    try:
                        signal = next(execution)
                    except StopIteration as finished:
                        result, shadow_artifact = finished.value
                        break
                    try:
                        if signal.event_type == "MODEL_ATTEMPT":
                            attempt = signal.payload
                            if (
                                not attempt.attempt_key
                                or not attempt.operation
                                or not attempt.request_hash
                                or not attempt.status
                            ):
                                raise InvalidAgentResult(
                                    "Model Attempt metadata is incomplete"
                                )
                            event = self._event(
                                request,
                                sequence,
                                "MODEL_ATTEMPT",
                                model_attempt=attempt,
                            )
                        elif signal.event_type == "EVIDENCE_APPENDED":
                            items = tuple(signal.payload)
                            if len(items) > self._max_evidence_items:
                                raise InvalidAgentResult(
                                    "Evidence count exceeds limit"
                                )
                            event = self._event(
                                request,
                                sequence,
                                "EVIDENCE_APPENDED",
                                evidence_items=items,
                            )
                        elif signal.event_type == "RUN_ARTIFACT_SAVED":
                            item = signal.payload
                            if (
                                not item.artifact_key
                                or not item.artifact_type
                                or not item.request_hash
                                or not item.content_hash
                                or not item.content
                                or len(item.content) > self._max_run_artifact_bytes
                            ):
                                raise InvalidAgentResult(
                                    "Run Artifact payload is invalid"
                                )
                            event = self._event(
                                request,
                                sequence,
                                "RUN_ARTIFACT_SAVED",
                                run_artifact=item,
                            )
                        elif signal.event_type == "CHECKPOINT_SAVED":
                            checkpoint_sequence, checkpoint = signal.payload
                            if (
                                checkpoint_sequence
                                <= latest_checkpoint_sequence
                                or not checkpoint
                                or len(checkpoint) > self._max_checkpoint_bytes
                            ):
                                raise InvalidAgentResult(
                                    "Checkpoint sequence or payload is invalid"
                                )
                            event = self._event(
                                request,
                                sequence,
                                "CHECKPOINT_SAVED",
                                checkpoint_sequence=checkpoint_sequence,
                                checkpoint=checkpoint,
                            )
                        elif signal.event_type in {"LEDGER_RESERVED", "LEDGER_CALL_STARTED", "LEDGER_FINISHED"}:
                            item = signal.payload
                            if not item.operation_key or not item.operation or not item.request_hash or not item.entry_kind:
                                raise InvalidAgentResult("Ledger event identity is incomplete")
                            event = self._event(
                                request,
                                sequence,
                                signal.event_type,
                                ledger_event=execution_proto.RunLedgerEvent(event_type=signal.event_type, entry=item),
                            )
                        else:
                            raise InvalidAgentResult(
                                f"unsupported runtime event: {signal.event_type}"
                            )
                        yield from self._yield_acked(
                            event,
                            context,
                        )
                        if signal.event_type == "CHECKPOINT_SAVED":
                            latest_checkpoint_sequence = checkpoint_sequence
                    except BaseException:
                        cancel_event.cancel()
                        raise
                    finally:
                        signal.released.set()
                    sequence += 1
                if cancel_event.is_cancelled() or not context.is_active():
                    yield from self._yield_acked(
                        self._event(
                            request,
                            sequence,
                            "RUN_FAILED",
                            error_category="CANCELLED",
                            retryable=False,
                        ),
                        context,
                    )
                    return
                self._validate_result(result, latest_checkpoint_sequence)
                if (
                    result.submission_disposition
                    is SubmissionDisposition.TERMINAL_ACK_ONLY
                    and run_context.submitted_draft is None
                ):
                    raise InvalidAgentResult(
                        "Terminal-only result requires a submitted draft receipt"
                    )
                attempts = (
                    *((result.attempt,) if result.attempt is not None else ()),
                    *result.additional_attempts,
                )
                for attempt in attempts:
                    yield from self._yield_acked(
                        self._event(request, sequence, "MODEL_ATTEMPT", model_attempt=attempt),
                        context,
                    )
                    sequence += 1
                if result.evidence:
                    yield from self._yield_acked(
                        self._event(
                            request,
                            sequence,
                            "EVIDENCE_APPENDED",
                            evidence_items=result.evidence,
                        ),
                        context,
                    )
                    sequence += 1
                if result.checkpoint_sequence is not None:
                    yield from self._yield_acked(
                        self._event(
                            request,
                            sequence,
                            "CHECKPOINT_SAVED",
                            checkpoint_sequence=result.checkpoint_sequence,
                            checkpoint=result.checkpoint,
                        ),
                        context,
                    )
                    sequence += 1
                if result.submission_disposition is SubmissionDisposition.SUBMIT_REQUIRED and result.draft_key is not None and result.expected_task_version is not None:
                    yield from self._yield_acked(
                        self._event(
                            request,
                            sequence,
                            "DRAFT_SUBMITTED",
                            draft_key=result.draft_key,
                            expected_task_version=result.expected_task_version,
                            draft_patch=result.draft_patch,
                        ),
                        context,
                    )
                    sequence += 1
                if (
                    result.submission_disposition
                    is SubmissionDisposition.SUBMIT_REQUIRED
                    and result.run_output is not None
                ):
                    yield from self._yield_acked(
                        self._event(
                            request,
                            sequence,
                            "RUN_OUTPUT_SUBMITTED",
                            run_output=result.run_output,
                        ),
                        context,
                    )
                    sequence += 1
                if shadow_artifact is not None:
                    if (
                        not shadow_artifact.artifact_key
                        or not shadow_artifact.artifact_type
                        or not shadow_artifact.request_hash
                        or not shadow_artifact.content_hash
                        or not shadow_artifact.content
                        or len(shadow_artifact.content) > self._max_run_artifact_bytes
                    ):
                        raise InvalidAgentResult("Shadow Artifact payload is invalid")
                    yield from self._yield_acked(
                        self._event(
                            request,
                            sequence,
                            "RUN_ARTIFACT_SAVED",
                            run_artifact=shadow_artifact,
                        ),
                        context,
                    )
                    sequence += 1
                yield from self._yield_acked(
                    self._event(request, sequence, "RUN_COMPLETED", result_type="SUCCEEDED"),
                    context,
                )
            except EventAckError:
                raise
            except Exception as error:  # the Go side owns durable failure state
                category, retryable = self._error_details(error)
                yield from self._yield_acked(
                    self._event(
                        request,
                        sequence,
                        "RUN_FAILED",
                        error_category=category,
                        retryable=retryable,
                    ),
                    context,
                )
        finally:
            with self._active_lock:
                self._active.pop(request.dispatch_id, None)
            self._slots.release()

    def AcknowledgeEvent(self, request, context):  # noqa: N802 - generated RPC name
        self._authorize(context)
        if (
            not self._valid_meta(request.meta)
            or not request.dispatch_id
            or not request.run_id
            or not request.event_id
        ):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "complete event acknowledgement identity is required")
        if request.worker_id != self._worker_id:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "worker identity mismatch")
        with self._ack_lock:
            expected = self._ack_waiters.get(request.event_id)
        if expected is None:
            return worker.AcknowledgeEventResponse(accepted=False)
        dispatch_id, run_id, sequence, waiter = expected
        if (
            request.dispatch_id != dispatch_id
            or request.run_id != run_id
            or request.event_sequence != sequence
            or request.committed_sequence < sequence
        ):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "event acknowledgement does not match pending event")
        waiter.set()
        return worker.AcknowledgeEventResponse(accepted=True)

    def CancelRun(self, request, context):  # noqa: N802 - generated RPC name
        self._authorize(context)
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
        self._authorize(context)
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
            supported_workflow_versions=sorted(self._supported_workflow_versions),
            supported_snapshot_schema_versions=(
                "agent-loop-snapshot.v1",
                "agent-loop-snapshot.v2",
                "agent-loop-snapshot.v3",
            ),
            supported_execution_ledger_versions=sorted(self._supported_execution_ledger_versions),
        )

    def _authorize(self, context) -> None:
        if self._service_token is None:
            return
        authorization = ""
        for item in context.invocation_metadata():
            key = item[0] if isinstance(item, tuple) else item.key
            value = item[1] if isinstance(item, tuple) else item.value
            if key.lower() == "authorization":
                authorization = value
                break
        expected = "Bearer " + self._service_token
        if not secrets.compare_digest(authorization, expected):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "service identity is invalid")

    @staticmethod
    def _valid_meta(meta) -> bool:
        return bool(
            meta
            and meta.contract_version == CONTRACT_VERSION
            and meta.request_id
            and meta.correlation_id
        )

    def _yield_acked(self, event, context):
        if self._event_ack_timeout_seconds <= 0:
            yield event
            return
        waiter = threading.Event()
        identity = (event.dispatch_id, event.run_id, event.event_sequence, waiter)
        with self._ack_lock:
            if event.event_id in self._ack_waiters:
                raise EventAckError(f"duplicate pending Agent event {event.event_id}")
            self._ack_waiters[event.event_id] = identity
        try:
            yield event
            if not waiter.wait(timeout=self._event_ack_timeout_seconds):
                if not context.is_active():
                    raise EventAckError(f"Agent event stream closed before ACK for {event.event_id}")
                raise EventAckError(f"timed out waiting for durable ACK for {event.event_id}")
        finally:
            with self._ack_lock:
                self._ack_waiters.pop(event.event_id, None)

    def _invoke_loop_with_events(
        self,
        run_context: RunContext,
        cancel_event: CancellationToken,
    ):
        signals: Queue[_RuntimeSignal] = Queue()
        done = threading.Event()
        result_box: dict[str, object] = {}

        def emit(event_type: str, payload) -> None:
            signal = _RuntimeSignal(event_type, payload)
            signals.put(signal)
            while not signal.released.wait(timeout=0.1):
                cancel_event.raise_if_cancelled()

        class QueueRuntimeEventSink(RuntimeEventSink):
            def model_attempt(self, attempt) -> None:
                emit("MODEL_ATTEMPT", attempt)

            def evidence(self, items) -> None:
                emit("EVIDENCE_APPENDED", tuple(items))

            def artifact(self, item) -> None:
                emit("RUN_ARTIFACT_SAVED", item)

            def checkpoint(self, sequence: int, payload: bytes) -> None:
                emit("CHECKPOINT_SAVED", (sequence, payload))

            def ledger(self, event_type: str, entry) -> None:
                emit(event_type, entry)

        def run_loop() -> None:
            try:
                observed_context = replace(
                    run_context,
                    plan_model_attempt=None,
                    event_sink=QueueRuntimeEventSink(),
                )
                result = self._invoke_loop(observed_context, cancel_event)
                shadow_artifact = ShadowArtifactModule().build(observed_context, result)
                result_box["result"] = result
                result_box["shadow_artifact"] = shadow_artifact
            except BaseException as error:
                result_box["error"] = error
            finally:
                done.set()

        thread = threading.Thread(target=run_loop, name=f"agent-loop-{run_context.run_id}", daemon=True)
        thread.start()
        while not done.is_set() or not signals.empty():
            try:
                yield signals.get(timeout=0.05)
            except Empty:
                continue
        thread.join(timeout=1)
        error = result_box.get("error")
        if isinstance(error, BaseException):
            raise error
        result = result_box.get("result")
        if not isinstance(result, AgentResult):
            raise InvalidAgentResult("Agent Loop did not return AgentResult")
        return result, result_box.get("shadow_artifact")

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
        attempts = (
            *((result.attempt,) if result.attempt is not None else ()),
            *result.additional_attempts,
        )
        for attempt in attempts:
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
        has_run_output = result.run_output is not None
        if result.submission_disposition is SubmissionDisposition.TERMINAL_ACK_ONLY:
            if has_draft or has_run_output or result.expected_task_version is not None:
                raise InvalidAgentResult("Terminal-only result must not contain an output")
            return
        if has_draft and has_run_output:
            raise InvalidAgentResult("Agent result cannot contain draft and run output")
        if has_draft and (
            not result.draft_key
            or result.expected_task_version is None
            or result.expected_task_version < 1
            or not result.draft_patch
        ):
            raise InvalidAgentResult("Draft result is incomplete")
        if has_run_output:
            output = result.run_output
            if (
                not output.schema_version
                or not output.output_key
                or output.output_kind
                == execution_proto.RUN_OUTPUT_KIND_UNSPECIFIED
                or output.run_purpose == execution_proto.RUN_PURPOSE_UNSPECIFIED
                or not output.scope_hash
                or output.expected_task_version < 1
                or not output.content_hash
                or not output.payload
                or len(output.payload) > self._max_draft_bytes
            ):
                raise InvalidAgentResult("Run output is incomplete")

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
        artifact = kwargs.get("run_artifact")
        if artifact is not None:
            result.run_artifact.CopyFrom(
                worker.RunArtifactEvent(
                    artifact_key=artifact.artifact_key,
                    artifact_type=artifact.artifact_type,
                    generation=artifact.generation,
                    request_hash=artifact.request_hash,
                    content_hash=artifact.content_hash,
                    content=artifact.content,
                )
            )
        ledger_event = kwargs.get("ledger_event")
        if ledger_event is not None:
            result.ledger_event.CopyFrom(ledger_event)
        run_output = kwargs.get("run_output")
        if run_output is not None:
            result.run_output.CopyFrom(run_output)
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
