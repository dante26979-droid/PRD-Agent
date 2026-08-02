import threading
import time

import grpc
import pytest

from agent.v1 import agent_execution_pb2 as execution
from agent.v1 import agent_worker_pb2 as worker
from agent.result import AgentResult, SubmissionDisposition
from agent.worker_server import AgentWorkerServer


class ActiveContext:
    def is_active(self):
        return True

    def abort(self, code, details):
        raise AssertionError(f"unexpected gRPC abort: {code} {details}")

    def invocation_metadata(self):
        return ()


class AuthenticatedContext(ActiveContext):
    def __init__(self, token):
        self.token = token

    def invocation_metadata(self):
        return (("authorization", f"Bearer {self.token}"),)


class Aborted(RuntimeError):
    def __init__(self, code):
        self.code = code


class RejectingContext(AuthenticatedContext):
    def abort(self, code, details):
        raise Aborted(code)


def _request(dispatch_id="dispatch-1", worker_id="worker-a"):
    return worker.ExecuteRunRequest(
        meta=execution.RequestMeta(
            contract_version="agent-execution.v2",
            request_id="request-1",
            correlation_id="run-1",
        ),
        dispatch_id=dispatch_id,
        run_id="run-1",
        worker_id=worker_id,
        lease=execution.LeaseContext(
            run_id="run-1",
            lease_id="lease-1",
            worker_id=worker_id,
            fencing_token=1,
            expires_at="2030-01-01T00:00:00Z",
        ),
        input=execution.AgentRunInput(
            run_id="run-1",
            tenant_id="tenant-1",
            owner_id="owner-1",
            task_id="task-1",
            task_message="write a PRD",
            workflow_version="agent-runtime.v1",
            task_version=3,
        ),
    )


def test_worker_server_streams_agent_progress_to_go_client():
    def loop(context):
        return AgentResult(
            attempt=execution.RecordModelAttemptRequest(
                attempt_key="attempt-1",
                operation="draft",
                request_hash="hash-1",
                status="SUCCEEDED",
            ),
            evidence=(
                execution.EvidenceItem(
                    source_type="github",
                    source_id="repo-1",
                    locator="README.md:1",
                    excerpt_hash="excerpt-1",
                ),
            ),
            checkpoint_sequence=1,
            checkpoint=b"checkpoint",
            draft_key="draft-1",
            expected_task_version=1,
            draft_patch=b"{\"title\":\"PRD\"}",
        )

    servicer = AgentWorkerServer(loop, worker_id="worker-a")
    responses = list(
        servicer.ExecuteRun(
            _request(),
            ActiveContext(),
        )
    )

    assert [item.event_type for item in responses] == [
        "RUN_STARTED",
        "MODEL_ATTEMPT",
        "EVIDENCE_APPENDED",
        "CHECKPOINT_SAVED",
        "DRAFT_SUBMITTED",
        "RUN_COMPLETED",
    ]
    assert responses[-1].result_type == "SUCCEEDED"
    assert responses[1].model_attempt.attempt_key == "attempt-1"
    assert responses[2].evidence_items[0].locator == "README.md:1"
    assert responses[3].checkpoint == b"checkpoint"
    assert responses[4].draft_key == "draft-1"


def test_worker_server_streams_execution_ledger_event_and_capability_version():
    def loop(context):
        assert context.execution_ledger_version == "run-ledger.v1"
        context.event_sink.ledger(
            "LEDGER_RESERVED",
            execution.RunLedgerEntry(
                operation_key="model:draft:sequence1",
                entry_kind="MODEL",
                operation="draft",
                request_hash="sha256:" + "a" * 64,
                status="RESERVED",
                reservation=execution.BudgetDelta(model_attempts=1),
            ),
        )
        return AgentResult()

    request = _request()
    request.input.workflow_version = "agent-runtime.v4"
    request.input.execution_ledger_version = "run-ledger.v1"
    request.input.run_budget.CopyFrom(execution.RunBudget(max_model_attempts=1))
    request.input.consumed_budget.CopyFrom(execution.ConsumedBudget())
    servicer = AgentWorkerServer(loop, worker_id="worker-a")

    responses = list(servicer.ExecuteRun(request, ActiveContext()))
    health = servicer.Health(
        worker.HealthRequest(
            meta=execution.RequestMeta(
                contract_version="agent-execution.v2",
                request_id="health-ledger",
                correlation_id="health-ledger",
            ),
            worker_id="worker-a",
        ),
        ActiveContext(),
    )

    assert [item.event_type for item in responses] == [
        "RUN_STARTED",
        "LEDGER_RESERVED",
        "RUN_COMPLETED",
    ]
    assert responses[1].ledger_event.entry.operation_key == "model:draft:sequence1"
    assert list(health.supported_execution_ledger_versions) == ["run-ledger.v1"]


def test_worker_server_streams_versioned_run_output_without_legacy_draft():
    def loop(context):
        assert context.run_purpose == execution.RUN_PURPOSE_PLAN_OUTLINE
        assert context.unit_scope.scope_hash == "a" * 64
        return AgentResult(
            run_output=execution.RunOutput(
                schema_version="run-output.v1",
                output_key="outline-1",
                output_kind=execution.RUN_OUTPUT_KIND_OUTLINE_CANDIDATE,
                run_purpose=execution.RUN_PURPOSE_PLAN_OUTLINE,
                scope_hash="a" * 64,
                expected_task_version=3,
                content_hash="b" * 64,
                payload=b'{"schema_version":"outline-candidate.v1"}',
            )
        )

    request = _request()
    request.input.workflow_version = "agent-runtime.v4"
    request.input.run_purpose = execution.RUN_PURPOSE_PLAN_OUTLINE
    request.input.unit_scope.CopyFrom(
        execution.UnitScope(
            schema_version="unit-scope.v1",
            scope_hash="a" * 64,
        )
    )
    servicer = AgentWorkerServer(loop, worker_id="worker-a")

    responses = list(servicer.ExecuteRun(request, ActiveContext()))

    assert [item.event_type for item in responses] == [
        "RUN_STARTED",
        "RUN_OUTPUT_SUBMITTED",
        "RUN_COMPLETED",
    ]
    assert responses[1].run_output.output_key == "outline-1"
    assert not responses[1].draft_patch


def test_worker_server_persists_hash_bound_shadow_artifact_on_real_path():
    def loop(context):
        assert context.evaluation_mode == "SHADOW"
        assert context.assignment_hash == "sha256:assignment"
        return AgentResult(
            draft_key="draft-shadow",
            expected_task_version=3,
            draft_patch=b'[{"op":"add","path":"/title","value":"Shadow"}]',
        )

    request = _request()
    request.input.evaluation_mode = "SHADOW"
    request.input.authoritative_workflow_version = "agent-runtime.v1"
    request.input.shadow_workflow_version = "agent-runtime.v4"
    request.input.candidate_policy_version = "policy-v4"
    request.input.assignment_hash = "sha256:assignment"
    servicer = AgentWorkerServer(loop, worker_id="worker-a")

    responses = list(servicer.ExecuteRun(request, ActiveContext()))

    assert [item.event_type for item in responses] == [
        "RUN_STARTED",
        "DRAFT_SUBMITTED",
        "RUN_ARTIFACT_SAVED",
        "RUN_COMPLETED",
    ]
    artifact = responses[2].run_artifact
    assert artifact.artifact_type == "SHADOW_EVALUATION"
    assert b'"extra_model_physical_calls":0' in artifact.content


def test_worker_server_terminal_ack_only_skips_duplicate_draft_event():
    servicer = AgentWorkerServer(
        lambda context: AgentResult(
            submission_disposition=SubmissionDisposition.TERMINAL_ACK_ONLY
        ),
        worker_id="worker-a",
    )
    request = _request()
    request.input.submitted_draft.CopyFrom(
        execution.SubmittedDraftReceipt(
            draft_key="run-1:draft:1",
            content_hash="sha256:" + "a" * 64,
            task_version=4,
            content=b"draft",
        )
    )
    responses = list(servicer.ExecuteRun(request, ActiveContext()))
    assert [item.event_type for item in responses] == [
        "RUN_STARTED",
        "RUN_COMPLETED",
    ]


def test_worker_server_rejects_missing_or_invalid_service_identity():
    servicer = AgentWorkerServer(
        lambda context: None,
        worker_id="worker-a",
        service_token="service-token-with-at-least-32-bytes",
    )
    for token in ("", "wrong-token"):
        with pytest.raises(Aborted) as captured:
            list(servicer.ExecuteRun(_request(), RejectingContext(token)))
        assert captured.value.code == grpc.StatusCode.UNAUTHENTICATED


def test_worker_server_cancel_emits_retry_safe_failed_terminal_event():
    started = threading.Event()

    def loop(context, cancel_event):
        started.set()
        cancel_event.wait(timeout=1)
        return AgentResult(
            attempt=execution.RecordModelAttemptRequest(
                attempt_key="attempt-1", operation="draft", request_hash="hash-1", status="SUCCEEDED"
            )
        )

    servicer = AgentWorkerServer(loop, worker_id="worker-a")
    responses = []

    def consume():
        responses.extend(list(servicer.ExecuteRun(_request(), ActiveContext())))

    thread = threading.Thread(target=consume)
    thread.start()
    assert started.wait(timeout=1)
    time.sleep(0.01)
    cancelled = servicer.CancelRun(
        worker.CancelRunRequest(
            meta=execution.RequestMeta(
                contract_version="agent-execution.v2", request_id="cancel-1", correlation_id="run-1"
            ),
            dispatch_id="dispatch-1",
            run_id="run-1",
            worker_id="worker-a",
        ),
        ActiveContext(),
    )
    thread.join(timeout=1)

    assert cancelled.accepted is True
    assert responses[-1].event_type == "RUN_FAILED"
    assert responses[-1].error_category == "CANCELLED"


def test_worker_server_propagates_versioned_context_and_trace_metadata():
    captured = {}

    def loop(context):
        captured["context"] = context
        return AgentResult(
            attempt=execution.RecordModelAttemptRequest(
                attempt_key="attempt-1",
                operation="draft",
                request_hash="hash-1",
                status="SUCCEEDED",
            )
        )

    servicer = AgentWorkerServer(
        loop,
        worker_id="worker-a",
        model_ready=True,
        capability_ready=False,
    )
    responses = list(servicer.ExecuteRun(_request(), ActiveContext()))
    health = servicer.Health(
        worker.HealthRequest(
            meta=execution.RequestMeta(
                contract_version="agent-execution.v2",
                request_id="health-1",
                correlation_id="health-1",
            ),
            worker_id="worker-a",
        ),
        ActiveContext(),
    )

    assert captured["context"].task_version == 3
    assert all(item.occurred_at for item in responses)
    assert all(item.contract_version == "agent-execution.v2" for item in responses)
    assert all(item.correlation_id == "run-1" for item in responses)
    assert health.model_ready is True
    assert health.capability_ready is False
    assert list(health.supported_workflow_versions) == [
        "agent-runtime.v1",
        "agent-runtime.v4",
    ]
    assert "agent-loop-snapshot.v3" in health.supported_snapshot_schema_versions


def test_worker_server_rejects_oversized_agent_result_without_success():
    def loop(context):
        return AgentResult(
            attempt=execution.RecordModelAttemptRequest(
                attempt_key="attempt-1",
                operation="draft",
                request_hash="hash-1",
                status="SUCCEEDED",
            ),
            draft_key="draft-1",
            expected_task_version=1,
            draft_patch=b"x" * 33,
        )

    responses = list(
        AgentWorkerServer(
            loop,
            worker_id="worker-a",
            max_draft_bytes=32,
        ).ExecuteRun(_request(), ActiveContext())
    )

    assert responses[-1].event_type == "RUN_FAILED"
    assert responses[-1].error_category == "INVALID_AGENT_RESULT"
    assert responses[-1].retryable is False
    assert all(item.event_type != "RUN_COMPLETED" for item in responses)


def test_worker_server_streams_each_model_attempt_in_order():
    def loop(context):
        return AgentResult(
            attempt=execution.RecordModelAttemptRequest(
                attempt_key="plan-1",
                operation="plan_investigation",
                request_hash="hash-plan",
                status="SUCCEEDED",
            ),
            additional_attempts=(
                execution.RecordModelAttemptRequest(
                    attempt_key="draft-1",
                    operation="generate_working_draft",
                    request_hash="hash-draft",
                    status="SUCCEEDED",
                ),
            ),
        )

    responses = list(
        AgentWorkerServer(loop, worker_id="worker-a").ExecuteRun(
            _request(),
            ActiveContext(),
        )
    )
    attempts = [item.model_attempt.operation for item in responses if item.event_type == "MODEL_ATTEMPT"]

    assert attempts == ["plan_investigation", "generate_working_draft"]


def test_worker_server_waits_for_durable_event_ack_before_advancing():
    servicer = AgentWorkerServer(
        lambda context: AgentResult(
            attempt=execution.RecordModelAttemptRequest(
                attempt_key="attempt-1",
                operation="draft",
                request_hash="hash-1",
                status="SUCCEEDED",
            )
        ),
        worker_id="worker-a",
        event_ack_timeout_seconds=1,
    )
    responses = []
    finished = threading.Event()

    def consume():
        try:
            for event in servicer.ExecuteRun(_request(), ActiveContext()):
                responses.append(event)
        finally:
            finished.set()

    thread = threading.Thread(target=consume)
    thread.start()
    deadline = time.time() + 1
    while len(responses) < 1 and time.time() < deadline:
        time.sleep(0.005)
    assert len(responses) == 1
    time.sleep(0.02)
    assert len(responses) == 1

    acknowledged = 0
    while not finished.is_set():
        while acknowledged < len(responses):
            event = responses[acknowledged]
            receipt = servicer.AcknowledgeEvent(
                worker.AcknowledgeEventRequest(
                    meta=execution.RequestMeta(
                        contract_version="agent-execution.v2",
                        request_id=f"ack-{event.event_sequence}",
                        correlation_id="run-1",
                    ),
                    dispatch_id=event.dispatch_id,
                    run_id=event.run_id,
                    event_id=event.event_id,
                    event_sequence=event.event_sequence,
                    committed_sequence=event.event_sequence,
                    worker_id="worker-a",
                ),
                ActiveContext(),
            )
            assert receipt.accepted is True
            acknowledged += 1
        time.sleep(0.005)
    thread.join(timeout=1)
    assert [item.event_type for item in responses] == [
        "RUN_STARTED",
        "MODEL_ATTEMPT",
        "RUN_COMPLETED",
    ]
