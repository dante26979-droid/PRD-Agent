"""Phase 3/5 must add durable Information Need and effective replanning."""

import json

from agent.checkpoint import CheckpointCodec
from agent.context import RunContext
from agent.graph import LangGraphAgentLoop
from agent.investigation import InvestigationBudget
from agent.quality import DraftQualityPolicy
from agent.runtime import BufferedRuntimeEventSink
from agent.testing import InstrumentedCapabilityGateway, ScriptedAgentModel


def _context(sink, run_id="char-need"):
    return RunContext(
        run_id=run_id,
        tenant_id="eval",
        owner_id="eval",
        task_id="char",
        task_message="new feature with no current-state dependency",
        workflow_version="agent-runtime.v1",
        checkpoint=b"",
        repository_binding_id="demo",
        repository_revision="a" * 40,
        event_sink=sink,
    )


def test_known_limit_direct_generation_has_no_information_need_artifact():
    sink = BufferedRuntimeEventSink()
    gateway = InstrumentedCapabilityGateway()
    result = LangGraphAgentLoop(
        model=ScriptedAgentModel([{"markdown": "# PRD\n\nDirect draft."}]),
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda _context: gateway,
        required_coverage=(),
    )(_context(sink))

    assert json.loads(result.draft_patch)["result_outcome"] == "EMPTY_EVIDENCE"
    assert gateway.observations == []
    assert all(item.artifact_type != "INFORMATION_NEED" for item in sink.artifacts)


def test_known_limit_duplicate_action_increments_replan_without_strategy_change():
    action = {
        "action": {
            "tool_id": "search_repository",
            "arguments": {"query": "missing"},
            "purpose": "repeat",
            "target_coverage": ["repository_evidence"],
        }
    }
    gateway = InstrumentedCapabilityGateway(repository_hits={"missing": ()})
    sink = BufferedRuntimeEventSink()
    result = LangGraphAgentLoop(
        model=ScriptedAgentModel(
            [action, action, {"markdown": "# PRD\n\nUnknown remains."}]
        ),
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda _context: gateway,
        budget=InvestigationBudget(max_iterations=3, max_tool_calls=3, max_replans=1),
    )(_context(sink, "char-replan"))
    final = CheckpointCodec().decode(sink.checkpoints[-1][1]).payload["snapshot"]

    assert len(gateway.observations) == 1
    assert final["replan_count"] == 1
    assert json.loads(result.draft_patch)["result_outcome"] == "EMPTY_EVIDENCE"
