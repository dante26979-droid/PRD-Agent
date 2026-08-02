"""Phase 4 must replace these expectations with Fact-based Coverage."""

from datetime import datetime, timezone

from agent.capability import RepositorySearchHit
from agent.checkpoint import CheckpointCodec
from agent.context import RunContext
from agent.eval_adapter import TraceRuntimeEventSink, build_trace_payload
from agent.graph import LangGraphAgentLoop
from agent.quality import DraftQualityPolicy
from agent.testing import InstrumentedCapabilityGateway, ScriptedAgentModel


def test_known_limit_unrelated_nonempty_evidence_marks_coverage_as_covered():
    model = ScriptedAgentModel(
        [
            {
                "action": {
                    "tool_id": "search_repository",
                    "arguments": {"query": "unrelated"},
                    "purpose": "current characterization",
                    "target_coverage": ["repository_evidence"],
                }
            },
            {"markdown": "# PRD\n\nEvidence is unrelated to the requested permission."},
        ]
    )
    gateway = InstrumentedCapabilityGateway(
        repository_hits={
            "unrelated": (RepositorySearchHit("README.md", 1, "project title"),)
        }
    )
    sink = TraceRuntimeEventSink()
    context = RunContext(
        run_id="char-coverage",
        tenant_id="eval",
        owner_id="eval",
        task_id="case-char",
        task_message="determine order cancellation permission",
        workflow_version="agent-runtime.v1",
        checkpoint=b"",
        repository_binding_id="demo",
        repository_revision="a" * 40,
        event_sink=sink,
    )
    result = LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda _context: gateway,
    )(context)

    trace = build_trace_payload(
        sink=sink,
        capability_observations=gateway.observations,
        result=result,
        eval_run_id="char-coverage",
        case_id="case-char",
        workflow_version="agent-runtime.v1",
        execution_mode="scripted_characterization",
        deterministic_only=True,
        started_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
        duration_ms=0,
        input_hash="sha256:char",
    )

    transition = trace["coverage_transitions"][0]
    assert transition["after"] == "COVERED"  # known limitation
    assert transition["evidence_delta"] == 1
    assert transition["fact_delta"] is None
