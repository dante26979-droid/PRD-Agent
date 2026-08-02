"""Phase 2/6 must remove shadow side effects and deepen quality rules."""

from agent.checkpoint import CheckpointCodec
from agent.context import RunContext
from agent.draft import DraftBundle, DraftUnit
from agent.graph import LangGraphAgentLoop
from agent.grounding import GroundingOutcome
from agent.quality import DraftQualityPolicy, QualityOutcome, check_advanced_quality
from agent.testing import InstrumentedCapabilityGateway, ScriptedAgentModel


def _context(run_id):
    return RunContext(
        run_id=run_id,
        tenant_id="eval",
        owner_id="eval",
        task_id="char",
        task_message="shadow characterization",
        workflow_version="agent-runtime.v1",
        checkpoint=b"",
        repository_binding_id="demo",
        repository_revision="a" * 40,
    )


def _structured_output():
    return {
        "markdown": "## Current\n\nUnsupported state.",
        "units": [
            {
                "unit_key": "current",
                "title": "Current",
                "order": 10,
                "markdown": "## Current\n\nUnsupported state.",
            }
        ],
        "claims": [
            {
                "unit_key": "current",
                "claim_type": "CURRENT_STATE",
                "criticality": "BLOCKING",
                "statement": "unsupported state",
            }
        ],
    }


def test_shadow_mode_has_zero_additional_remote_calls():
    off_gateway = InstrumentedCapabilityGateway()
    off_model = ScriptedAgentModel([_structured_output()])
    LangGraphAgentLoop(
        model=off_model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda _context: off_gateway,
        required_coverage=(),
        advanced_loop_mode="off",
    )(_context("char-shadow-off"))

    shadow_gateway = InstrumentedCapabilityGateway(
        repository_hits={"unsupported state": ()}
    )
    shadow_model = ScriptedAgentModel([_structured_output()])
    LangGraphAgentLoop(
        model=shadow_model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda _context: shadow_gateway,
        required_coverage=(),
        advanced_loop_mode="shadow",
    )(_context("char-shadow-on"))

    assert len(off_gateway.observations) == 0
    assert len(shadow_gateway.observations) == 0
    assert len(shadow_model.calls) == len(off_model.calls)


def test_known_limit_quality_passes_non_executable_acceptance_text():
    bundle = DraftBundle(
        schema_version="draft-bundle.v1",
        generation=1,
        units=(
            DraftUnit(
                "acceptance",
                "Acceptance",
                "## Acceptance\n\nThe system should behave reasonably.",
                10,
            ),
        ),
        claims=(),
        unknowns=(),
        markdown="## Acceptance\n\nThe system should behave reasonably.",
    )

    issues, outcome = check_advanced_quality(
        bundle,
        grounding_outcome=GroundingOutcome.GROUNDED,
        repair_count=0,
        max_repairs=1,
    )

    assert issues == ()
    assert outcome == QualityOutcome.PASSED  # known limitation
