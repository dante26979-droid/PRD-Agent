"""Phase 1 must bind repository revision and other resume invariants."""

from agent.checkpoint import CheckpointCodec
from agent.context import RunContext
from agent.graph import LangGraphAgentLoop
from agent.quality import DraftQualityPolicy
from agent.testing import InstrumentedCapabilityGateway, ScriptedAgentModel


def _context(run_id, revision, *, checkpoint=b"", sequence=0):
    return RunContext(
        run_id=run_id,
        tenant_id="eval",
        owner_id="eval",
        task_id="char",
        task_message="resume characterization",
        workflow_version="agent-runtime.v1",
        checkpoint=checkpoint,
        checkpoint_sequence=sequence,
        repository_binding_id="demo",
        repository_revision=revision,
    )


def test_known_limit_ready_snapshot_accepts_repository_revision_drift():
    loop = LangGraphAgentLoop(
        model=ScriptedAgentModel([{"markdown": "# PRD\n\nReady."}]),
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda _context: InstrumentedCapabilityGateway(),
        required_coverage=(),
    )
    first = loop(_context("char-resume", "a" * 40))

    resumed = LangGraphAgentLoop(
        model=ScriptedAgentModel([]),
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda _context: InstrumentedCapabilityGateway(),
        required_coverage=(),
    )(
        _context(
            "char-resume",
            "b" * 40,
            checkpoint=first.checkpoint,
            sequence=first.checkpoint_sequence,
        )
    )

    assert resumed.draft_patch == first.draft_patch  # known limitation
