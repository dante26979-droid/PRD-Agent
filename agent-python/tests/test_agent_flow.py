from __future__ import annotations

import json

from agent.bootstrap import build_agent_loop
from agent.checkpoint import CheckpointCodec
from agent.v1 import agent_execution_pb2 as execution
from agent.v1 import agent_worker_pb2 as worker
from agent.worker_server import AgentWorkerServer


class ActiveContext:
    def is_active(self):
        return True

    def abort(self, code, details):
        raise AssertionError(f"unexpected gRPC abort: {code} {details}")


def test_agent_worker_runs_default_runtime_from_go_request_to_draft(monkeypatch):
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "test")
    monkeypatch.delenv("PRD_AGENT_LLM_PROVIDER", raising=False)
    request = worker.ExecuteRunRequest(
        meta=execution.RequestMeta(
            contract_version="agent-execution.v1",
            request_id="dispatch-1",
            correlation_id="run-1",
        ),
        dispatch_id="dispatch-1",
        run_id="run-1",
        worker_id="worker-1",
        lease=execution.LeaseContext(
            run_id="run-1",
            lease_id="lease-1",
            worker_id="worker-1",
            fencing_token=1,
            expires_at="2030-01-01T00:00:00Z",
        ),
        input=execution.AgentRunInput(
            run_id="run-1",
            tenant_id="tenant-1",
            owner_id="owner-1",
            task_id="task-1",
            task_message="生成可恢复的 Agent Runtime PRD",
            workflow_version="agent-runtime.v1",
            task_version=1,
        ),
    )

    events = list(
        AgentWorkerServer(
            build_agent_loop(),
            worker_id="worker-1",
        ).ExecuteRun(request, ActiveContext())
    )

    checkpoint_event = next(item for item in events if item.event_type == "CHECKPOINT_SAVED")
    draft_event = next(item for item in events if item.event_type == "DRAFT_SUBMITTED")

    assert [item.event_type for item in events] == [
        "RUN_STARTED",
        "MODEL_ATTEMPT",
        "CHECKPOINT_SAVED",
        "DRAFT_SUBMITTED",
        "RUN_COMPLETED",
    ]
    assert CheckpointCodec().decode(checkpoint_event.checkpoint).run_id == "run-1"
    assert "可恢复" in json.loads(draft_event.draft_patch)["markdown"]
    assert events[-1].result_type == "SUCCEEDED"
