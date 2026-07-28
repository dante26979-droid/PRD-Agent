import threading
import time

from agent.v1 import agent_execution_pb2 as execution
from agent.v1 import agent_worker_pb2 as worker
from agent.result import AgentResult
from agent.worker_server import AgentWorkerServer


class ActiveContext:
    def is_active(self):
        return True

    def abort(self, code, details):
        raise AssertionError(f"unexpected gRPC abort: {code} {details}")


def _request(dispatch_id="dispatch-1", worker_id="worker-a"):
    return worker.ExecuteRunRequest(
        meta=execution.RequestMeta(
            contract_version="agent-execution.v1",
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
                contract_version="agent-execution.v1", request_id="cancel-1", correlation_id="run-1"
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
                contract_version="agent-execution.v1",
                request_id="health-1",
                correlation_id="health-1",
            ),
            worker_id="worker-a",
        ),
        ActiveContext(),
    )

    assert captured["context"].task_version == 3
    assert all(item.occurred_at for item in responses)
    assert all(item.contract_version == "agent-execution.v1" for item in responses)
    assert all(item.correlation_id == "run-1" for item in responses)
    assert health.model_ready is True
    assert health.capability_ready is False


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
