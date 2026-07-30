from agent.v1 import agent_execution_pb2 as proto
from agent.v1 import capability_gateway_pb2 as capability
from agent.v1 import agent_worker_pb2 as worker


def test_agent_worker_request_round_trips_lease_and_metadata():
    original = worker.ExecuteRunRequest(
        dispatch_id="dispatch-1",
        run_id="run-1",
        worker_id="worker-1",
        lease=proto.LeaseContext(
            run_id="run-1",
            lease_id="lease-1",
            worker_id="worker-1",
            fencing_token=1,
        ),
        meta=proto.RequestMeta(
            contract_version="agent-execution.v2",
            request_id="req-1",
            correlation_id="corr-1",
        ),
    )

    decoded = worker.ExecuteRunRequest.FromString(original.SerializeToString())

    assert decoded.dispatch_id == "dispatch-1"
    assert decoded.run_id == "run-1"
    assert decoded.worker_id == "worker-1"
    assert decoded.lease.lease_id == "lease-1"
    assert decoded.meta.contract_version == "agent-execution.v2"
    assert decoded.meta.correlation_id == "corr-1"


def test_contract_preserves_unknown_forward_compatible_field():
    original = proto.AgentRunInput(run_id="run-1", checkpoint_sequence=3)
    wire = original.SerializeToString() + b"\x48\x2a"  # future field 9 = 42

    decoded = proto.AgentRunInput.FromString(wire)

    assert decoded.run_id == "run-1"
    assert decoded.checkpoint_sequence == 3


def test_agent_worker_contract_round_trips_stream_event():
	original = worker.ExecuteRunResponse(
		dispatch_id="dispatch-1",
		run_id="run-1",
		event_sequence=2,
		event_id="event-2",
		event_type="MODEL_ATTEMPT",
		model_attempt=worker.ModelAttemptEvent(
			attempt_key="attempt-1", operation="draft", request_hash="hash-1", status="SUCCEEDED"
		),
	)
	decoded = worker.ExecuteRunResponse.FromString(original.SerializeToString())

	assert decoded.dispatch_id == "dispatch-1"
	assert decoded.event_sequence == 2
	assert decoded.model_attempt.attempt_key == "attempt-1"


def test_worker_event_and_capability_context_preserve_trace_metadata():
	event = worker.ExecuteRunResponse(
		dispatch_id="dispatch-1",
		run_id="run-1",
		event_sequence=1,
		event_id="event-1",
		event_type="RUN_STARTED",
		occurred_at="2026-07-28T12:00:00Z",
		contract_version="agent-execution.v2",
		correlation_id="correlation-1",
	)
	request = capability.SearchRepositoryRequest(
		capability=capability.CapabilityLease(
			lease=proto.LeaseContext(
				run_id="run-1",
				lease_id="lease-1",
				worker_id="worker-1",
				fencing_token=1,
			),
			contract_version="agent-execution.v1",
			meta=proto.RequestMeta(
				contract_version="agent-execution.v1",
				request_id="request-1",
				correlation_id="correlation-1",
			),
		),
		binding_id="binding-1",
		revision="a" * 40,
		query="dispatcher",
		limit=10,
	)

	decoded_event = worker.ExecuteRunResponse.FromString(event.SerializeToString())
	decoded_request = capability.SearchRepositoryRequest.FromString(request.SerializeToString())

	assert decoded_event.occurred_at == "2026-07-28T12:00:00Z"
	assert decoded_event.contract_version == "agent-execution.v2"
	assert decoded_event.correlation_id == "correlation-1"
	assert decoded_request.capability.meta.request_id == "request-1"
