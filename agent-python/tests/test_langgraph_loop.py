from __future__ import annotations

import json

import pytest

from agent.capability import RepositorySearchHit
from agent.cancellation import AgentCancelled, CancellationToken
from agent.checkpoint import CheckpointCodec, CheckpointError
from agent.context import Lease, RunContext
from agent.graph import LangGraphAgentLoop
from agent.investigation import InvestigationBudget
from agent.model import ModelResponse
from agent.quality import DraftQualityPolicy
from agent.runtime import BufferedRuntimeEventSink


class ScriptedModel:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def complete(self, system_prompt, user_prompt):
        self.calls.append(json.loads(user_prompt))
        if not self.outputs:
            raise AssertionError("unexpected model call")
        output = self.outputs.pop(0)
        tokens = int(output.pop("_tokens", 1))
        return ModelResponse(
            output=json.dumps(output, ensure_ascii=False),
            token_usage={"total_tokens": tokens},
            model_id="fake-model",
        )


class FakeRepositoryGateway:
    def __init__(self, hits_by_query):
        self.hits_by_query = hits_by_query
        self.queries = []
        self.closed = False

    def search_repository(self, **kwargs):
        self.queries.append(kwargs["query"])
        return tuple(self.hits_by_query.get(kwargs["query"], ()))

    def close(self):
        self.closed = True


def _context(*, checkpoint=b"", checkpoint_sequence=0, event_sink=None):
    return RunContext(
        run_id="run-loop",
        tenant_id="tenant-1",
        owner_id="owner-1",
        task_id="task-1",
        task_message="为订单筛选生成 PRD",
        workflow_version="agent-runtime.v2",
        checkpoint=checkpoint,
        checkpoint_sequence=checkpoint_sequence,
        task_version=1,
        dispatch_id="dispatch-1",
        lease=Lease(
            "run-loop",
            "lease-1",
            "worker-1",
            1,
            "2030-01-01T00:00:00Z",
        ),
        repository_binding_id="binding-1",
        repository_revision="a" * 40,
        event_sink=event_sink,
    )


def _action(query, coverage):
    return {
        "action": {
            "tool_id": "search_repository",
            "tool_schema_version": "1",
            "arguments": {"query": query},
            "purpose": f"调查 {coverage}",
            "target_coverage": [coverage],
        }
    }


def _hit(path, line, snippet):
    return RepositorySearchHit(path=path, line=line, snippet=snippet)


def test_langgraph_loop_uses_observation_to_select_a_second_action():
    model = ScriptedModel(
        _action("order route", "repository_structure"),
        _action("order tests", "tests"),
        {"markdown": "# PRD\n\n基于两轮仓库证据生成。"},
    )
    gateway = FakeRepositoryGateway(
        {
            "order route": (_hit("api/orders.py", 10, "def list_orders"),),
            "order tests": (_hit("tests/test_orders.py", 20, "created_at filter"),),
        }
    )
    sink = BufferedRuntimeEventSink()
    loop = LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda context: gateway,
        budget=InvestigationBudget(max_iterations=3, max_tool_calls=3),
        required_coverage=("repository_structure", "tests"),
    )

    result = loop(_context(event_sink=sink))
    draft = json.loads(result.draft_patch)
    stages = [
        CheckpointCodec().decode(payload).payload["stage"]
        for _, payload in sink.checkpoints
    ]

    assert gateway.queries == ["order route", "order tests"]
    assert model.calls[1]["observations"][0]["locator"].endswith(
        "/api/orders.py#L10"
    )
    assert draft["coverage"] == {
        "repository_structure": "COVERED",
        "tests": "COVERED",
    }
    assert draft["result_outcome"] == "DRAFT_READY"
    assert stages == [
        "ACTION_VALIDATED",
        "OBSERVED",
        "ACTION_VALIDATED",
        "OBSERVED",
        "INVESTIGATION_FINISHED",
        "READY_TO_SUBMIT",
    ]
    assert [sequence for sequence, _ in sink.checkpoints] == [1, 2, 3, 4, 5, 6]
    snapshots = [
        CheckpointCodec().decode(payload).payload for _, payload in sink.checkpoints
    ]
    assert all(
        item["snapshot_schema_version"] == "agent-loop-snapshot.v2"
        for item in snapshots
    )
    assert [item["status"] for item in snapshots] == stages
    assert snapshots[-1]["snapshot"]["candidate_markdown"] == draft["markdown"]
    assert snapshots[-1]["snapshot"]["status"] == "READY_TO_SUBMIT"


def test_duplicate_empty_action_stops_without_second_physical_capability_call():
    model = ScriptedModel(
        _action("missing symbol", "repository_evidence"),
        _action("missing symbol", "repository_evidence"),
        {"markdown": "# PRD\n\n未发现仓库证据，相关事实标记为 Unknown。"},
    )
    gateway = FakeRepositoryGateway({"missing symbol": ()})
    loop = LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda context: gateway,
        budget=InvestigationBudget(
            max_iterations=3,
            max_tool_calls=3,
            no_progress_limit=2,
            max_replans=1,
        ),
    )

    result = loop(_context())
    draft = json.loads(result.draft_patch)

    assert gateway.queries == ["missing symbol"]
    assert draft["stop_reason"] == "NO_PROGRESS"
    assert draft["result_outcome"] == "EMPTY_EVIDENCE"


def test_token_budget_stops_before_capability_and_returns_bounded_unknown_draft():
    model = ScriptedModel(
        {
            **_action("order route", "repository_evidence"),
            "_tokens": 5,
        }
    )
    gateway = FakeRepositoryGateway(
        {"order route": (_hit("api/orders.py", 1, "route"),)}
    )
    loop = LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda context: gateway,
        budget=InvestigationBudget(
            max_iterations=3,
            max_tool_calls=3,
            token_budget=5,
        ),
    )

    result = loop(_context())
    draft = json.loads(result.draft_patch)

    assert gateway.queries == []
    assert draft["stop_reason"] == "TOKEN_BUDGET_EXHAUSTED"
    assert "Unknown" in draft["markdown"]


def test_iteration_budget_is_a_deterministic_stop():
    model = ScriptedModel(
        _action("missing symbol", "repository_evidence"),
        {"markdown": "# PRD\n\n一次调查后按迭代预算停止。"},
    )
    gateway = FakeRepositoryGateway({"missing symbol": ()})
    loop = LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda context: gateway,
        budget=InvestigationBudget(
            max_iterations=1,
            max_tool_calls=3,
            no_progress_limit=2,
        ),
    )

    draft = json.loads(loop(_context()).draft_patch)

    assert gateway.queries == ["missing symbol"]
    assert draft["stop_reason"] == "MAX_ITERATIONS_REACHED"


def test_tool_budget_stops_before_a_second_coverage_action():
    model = ScriptedModel(
        _action("order route", "repository_structure"),
        {"markdown": "# PRD\n\n工具预算耗尽，测试覆盖保持 Unknown。"},
    )
    gateway = FakeRepositoryGateway(
        {"order route": (_hit("api/orders.py", 1, "route"),)}
    )
    loop = LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda context: gateway,
        budget=InvestigationBudget(max_iterations=3, max_tool_calls=1),
        required_coverage=("repository_structure", "tests"),
    )

    draft = json.loads(loop(_context()).draft_patch)

    assert gateway.queries == ["order route"]
    assert draft["stop_reason"] == "TOOL_BUDGET_EXHAUSTED"
    assert draft["coverage"]["tests"] == "MISSING"
    assert draft["result_outcome"] == "PARTIAL_EVIDENCE"


def test_cancelled_graph_stops_before_model_or_capability():
    model = ScriptedModel()
    gateway = FakeRepositoryGateway({})
    token = CancellationToken()
    token.cancel()
    loop = LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda context: gateway,
    )

    with pytest.raises(AgentCancelled):
        loop(_context(), token)

    assert model.calls == []
    assert gateway.queries == []


def test_resume_from_validated_action_does_not_repeat_model_selection():
    first_model = ScriptedModel(_action("order route", "repository_evidence"))
    first_gateway = FakeRepositoryGateway(
        {"order route": (_hit("api/orders.py", 1, "route"),)}
    )

    class CrashAfterCheckpoint(BufferedRuntimeEventSink):
        def checkpoint(self, sequence, payload):
            super().checkpoint(sequence, payload)
            raise RuntimeError("simulated worker crash")

    crashed_sink = CrashAfterCheckpoint()
    loop = LangGraphAgentLoop(
        model=first_model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda context: first_gateway,
    )

    with pytest.raises(RuntimeError, match="simulated worker crash"):
        loop(_context(event_sink=crashed_sink))

    sequence, checkpoint = crashed_sink.checkpoints[-1]
    assert CheckpointCodec().decode(checkpoint).payload["stage"] == "ACTION_VALIDATED"
    assert first_gateway.queries == []

    decoded = CheckpointCodec().decode(checkpoint)
    payload = dict(decoded.payload)
    snapshot = dict(payload["snapshot"])
    snapshot["phase"] = "OBSERVED"
    snapshot["status"] = "OBSERVED"
    payload["snapshot"] = snapshot
    checkpoint = CheckpointCodec().encode(
        workflow_version=decoded.workflow_version,
        run_id=decoded.run_id,
        task_version=decoded.task_version,
        sequence=decoded.sequence,
        payload=payload,
    )

    resumed_model = ScriptedModel(
        {"markdown": "# PRD\n\n从已校验动作的 checkpoint 恢复。"}
    )
    resumed_gateway = FakeRepositoryGateway(
        {"order route": (_hit("api/orders.py", 1, "route"),)}
    )
    resumed = LangGraphAgentLoop(
        model=resumed_model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda context: resumed_gateway,
    )(
        _context(
            checkpoint=checkpoint,
            checkpoint_sequence=sequence,
        )
    )

    assert resumed_gateway.queries == ["order route"]
    assert len(resumed_model.calls) == 1
    assert resumed_model.calls[0]["evidence"][0]["locator"].endswith(
        "/api/orders.py#L1"
    )
    assert "checkpoint 恢复" in json.loads(resumed.draft_patch)["markdown"]


def test_ready_to_submit_snapshot_restarts_without_model_tool_or_new_checkpoint():
    initial_model = ScriptedModel(
        {"markdown": "# PRD\n\n已完成且可以直接提交的快照。"}
    )
    initial_gateway = FakeRepositoryGateway({})
    initial = LangGraphAgentLoop(
        model=initial_model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda context: initial_gateway,
    )(_context())

    snapshot = CheckpointCodec().decode(initial.checkpoint)
    assert snapshot.payload["status"] == "READY_TO_SUBMIT"
    assert snapshot.payload["snapshot"]["candidate_markdown"]

    resumed_model = ScriptedModel()
    resumed_gateway = FakeRepositoryGateway({})
    resumed = LangGraphAgentLoop(
        model=resumed_model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda context: resumed_gateway,
    )(
        _context(
            checkpoint=initial.checkpoint,
            checkpoint_sequence=initial.checkpoint_sequence,
        )
    )

    assert resumed_model.calls == []
    assert resumed_gateway.queries == []
    assert resumed.attempt is None
    assert resumed.checkpoint_sequence == initial.checkpoint_sequence
    assert resumed.checkpoint == initial.checkpoint
    assert resumed.draft_patch == initial.draft_patch


def test_observed_snapshot_status_prevents_capability_replay_after_restart():
    first_model = ScriptedModel(_action("order route", "repository_evidence"))
    first_gateway = FakeRepositoryGateway(
        {"order route": (_hit("api/orders.py", 1, "route"),)}
    )

    class CrashAfterObservation(BufferedRuntimeEventSink):
        def checkpoint(self, sequence, payload):
            super().checkpoint(sequence, payload)
            status = CheckpointCodec().decode(payload).payload["status"]
            if status == "OBSERVED":
                raise RuntimeError("simulated crash after observation snapshot")

    crashed_sink = CrashAfterObservation()
    loop = LangGraphAgentLoop(
        model=first_model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda context: first_gateway,
    )

    with pytest.raises(RuntimeError, match="after observation snapshot"):
        loop(_context(event_sink=crashed_sink))

    sequence, checkpoint = crashed_sink.checkpoints[-1]
    assert CheckpointCodec().decode(checkpoint).payload["status"] == "OBSERVED"
    assert first_gateway.queries == ["order route"]

    resumed_model = ScriptedModel(
        {"markdown": "# PRD\n\n观察结果已持久化，不重复调用 Capability。"}
    )
    resumed_gateway = FakeRepositoryGateway({})
    resumed = LangGraphAgentLoop(
        model=resumed_model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda context: resumed_gateway,
    )(_context(checkpoint=checkpoint, checkpoint_sequence=sequence))

    assert resumed_gateway.queries == []
    assert len(resumed_model.calls) == 1
    assert resumed_model.calls[0]["evidence"][0]["locator"].endswith(
        "/api/orders.py#L1"
    )
    assert "不重复调用" in json.loads(resumed.draft_patch)["markdown"]


def test_unknown_snapshot_status_is_rejected_instead_of_guessing_resume_node():
    checkpoint = CheckpointCodec().encode(
        workflow_version="agent-runtime.v2",
        run_id="run-loop",
        task_version=1,
        sequence=1,
        payload={
            "snapshot_schema_version": "agent-loop-snapshot.v1",
            "status": "CAPABILITY_MAYBE_EXECUTED",
            "snapshot": {
                "run_id": "run-loop",
                "task_id": "task-1",
                "task_version": 1,
            },
        },
    )
    loop = LangGraphAgentLoop(
        model=ScriptedModel(),
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
    )

    with pytest.raises(CheckpointError, match="unsupported loop checkpoint status"):
        loop(_context(checkpoint=checkpoint, checkpoint_sequence=1))
