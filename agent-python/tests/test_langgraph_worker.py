from __future__ import annotations

import json
import threading
import time

from agent.capability import RepositorySearchHit
from agent.checkpoint import CheckpointCodec
from agent.graph import LangGraphAgentLoop
from agent.investigation import InvestigationBudget
from agent.model import ModelResponse
from agent.quality import DraftQualityPolicy
from agent.v1 import agent_execution_pb2 as execution
from agent.v1 import agent_worker_pb2 as worker
from agent.worker_server import AgentWorkerServer


class ActiveContext:
    def is_active(self):
        return True

    def abort(self, code, details):
        raise AssertionError(f"unexpected gRPC abort: {code} {details}")

    def invocation_metadata(self):
        return ()


class ScriptedModel:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def complete(self, system_prompt, user_prompt):
        self.calls += 1
        return ModelResponse(
            output=json.dumps(self.outputs.pop(0), ensure_ascii=False),
            token_usage={"total_tokens": 1},
            model_id="fake-model",
        )


class FakeGateway:
    def __init__(self):
        self.calls = 0

    def search_repository(self, **kwargs):
        self.calls += 1
        return (
            RepositorySearchHit(
                path="api/orders.py",
                line=10,
                snippet="def list_orders(created_at=None)",
            ),
        )

    def close(self):
        pass


def _request():
    return worker.ExecuteRunRequest(
        meta=execution.RequestMeta(
            contract_version="agent-execution.v2",
            request_id="request-1",
            correlation_id="run-loop",
        ),
        dispatch_id="dispatch-loop",
        run_id="run-loop",
        worker_id="worker-1",
        lease=execution.LeaseContext(
            run_id="run-loop",
            lease_id="lease-1",
            worker_id="worker-1",
            fencing_token=1,
            expires_at="2030-01-01T00:00:00Z",
        ),
        input=execution.AgentRunInput(
            run_id="run-loop",
            tenant_id="tenant-1",
            owner_id="owner-1",
            task_id="task-1",
            task_message="生成订单筛选 PRD",
            workflow_version="agent-runtime.v2",
            task_version=1,
            repository_binding_id="binding-1",
            repository_revision="a" * 40,
        ),
    )


def _loop(model, gateway):
    return LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda context: gateway,
        budget=InvestigationBudget(max_iterations=3, max_tool_calls=3),
    )


def _action():
    return {
        "action": {
            "tool_id": "search_repository",
            "arguments": {"query": "order created_at filter"},
            "purpose": "定位订单筛选实现",
            "target_coverage": ["repository_evidence"],
        }
    }


def test_worker_streams_langgraph_node_events_without_terminal_duplicates():
    model = ScriptedModel(
        _action(),
        {"markdown": "# PRD\n\n基于订单筛选证据生成。"},
    )
    gateway = FakeGateway()

    events = list(
        AgentWorkerServer(
            _loop(model, gateway),
            worker_id="worker-1",
        ).ExecuteRun(_request(), ActiveContext())
    )

    assert [item.event_type for item in events] == [
        "RUN_STARTED",
        "MODEL_ATTEMPT",
        "MODEL_ATTEMPT",
        "CHECKPOINT_SAVED",
        "EVIDENCE_APPENDED",
        "CHECKPOINT_SAVED",
        "CHECKPOINT_SAVED",
        "MODEL_ATTEMPT",
        "MODEL_ATTEMPT",
        "CHECKPOINT_SAVED",
        "DRAFT_SUBMITTED",
        "RUN_COMPLETED",
    ]
    assert [
        item.model_attempt.status
        for item in events
        if item.event_type == "MODEL_ATTEMPT"
    ] == ["PLANNED", "SUCCEEDED", "PLANNED", "SUCCEEDED"]
    assert [
        item.checkpoint_sequence
        for item in events
        if item.event_type == "CHECKPOINT_SAVED"
    ] == [1, 2, 3, 4]
    assert [
        CheckpointCodec().decode(item.checkpoint).payload["status"]
        for item in events
        if item.event_type == "CHECKPOINT_SAVED"
    ] == [
        "ACTION_VALIDATED",
        "OBSERVED",
        "INVESTIGATION_FINISHED",
        "READY_TO_SUBMIT",
    ]
    assert gateway.calls == 1


def test_checkpoint_action_ack_happens_before_capability_execution():
    model = ScriptedModel(
        _action(),
        {"markdown": "# PRD\n\nACK 后才执行 Capability。"},
    )
    gateway = FakeGateway()
    servicer = AgentWorkerServer(
        _loop(model, gateway),
        worker_id="worker-1",
        event_ack_timeout_seconds=1,
    )
    events = []
    finished = threading.Event()

    def consume():
        try:
            for event in servicer.ExecuteRun(_request(), ActiveContext()):
                events.append(event)
        finally:
            finished.set()

    thread = threading.Thread(target=consume)
    thread.start()

    def wait_for(count):
        deadline = time.time() + 1
        while len(events) < count and time.time() < deadline:
            time.sleep(0.005)
        assert len(events) >= count

    def acknowledge(index):
        event = events[index]
        receipt = servicer.AcknowledgeEvent(
            worker.AcknowledgeEventRequest(
                meta=execution.RequestMeta(
                    contract_version="agent-execution.v2",
                    request_id=f"ack-{event.event_sequence}",
                    correlation_id="run-loop",
                ),
                dispatch_id=event.dispatch_id,
                run_id=event.run_id,
                event_id=event.event_id,
                event_sequence=event.event_sequence,
                committed_sequence=event.event_sequence,
                worker_id="worker-1",
            ),
            ActiveContext(),
        )
        assert receipt.accepted is True

    wait_for(1)
    assert model.calls == 0
    acknowledge(0)

    wait_for(2)
    assert events[1].model_attempt.status == "PLANNED"
    assert model.calls == 0
    acknowledge(1)

    wait_for(3)
    assert events[2].model_attempt.status == "SUCCEEDED"
    assert gateway.calls == 0
    acknowledge(2)

    wait_for(4)
    assert events[3].event_type == "CHECKPOINT_SAVED"
    assert gateway.calls == 0
    acknowledge(3)

    wait_for(5)
    assert gateway.calls == 1

    acknowledged = 4
    while not finished.is_set():
        while acknowledged < len(events):
            acknowledge(acknowledged)
            acknowledged += 1
        time.sleep(0.005)
    thread.join(timeout=1)

    assert events[-1].event_type == "RUN_COMPLETED"


def test_worker_restart_uses_ready_snapshot_status_and_replays_only_draft():
    first_events = list(
        AgentWorkerServer(
            _loop(
                ScriptedModel({"markdown": "# PRD\n\n可从终态快照直接恢复。"}),
                FakeGateway(),
            ),
            worker_id="worker-1",
        ).ExecuteRun(_request(), ActiveContext())
    )
    ready = next(
        item
        for item in first_events
        if item.event_type == "CHECKPOINT_SAVED"
        and CheckpointCodec().decode(item.checkpoint).payload["status"]
        == "READY_TO_SUBMIT"
    )
    first_draft = next(
        item.draft_patch
        for item in first_events
        if item.event_type == "DRAFT_SUBMITTED"
    )

    request = _request()
    request.input.checkpoint = ready.checkpoint
    request.input.checkpoint_sequence = ready.checkpoint_sequence
    resumed_model = ScriptedModel()
    resumed_gateway = FakeGateway()
    resumed_events = list(
        AgentWorkerServer(
            _loop(resumed_model, resumed_gateway),
            worker_id="worker-1",
        ).ExecuteRun(request, ActiveContext())
    )

    assert [item.event_type for item in resumed_events] == [
        "RUN_STARTED",
        "DRAFT_SUBMITTED",
        "RUN_COMPLETED",
    ]
    assert resumed_events[1].draft_patch == first_draft
    assert resumed_model.calls == 0
    assert resumed_gateway.calls == 0
