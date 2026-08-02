"""Phase 4/5 must replace reference-presence and bulk-supplement behavior."""

import json

from agent.capability import RepositorySearchHit
from agent.checkpoint import CheckpointCodec
from agent.context import RunContext
from agent.draft import (
    Claim,
    ClaimCriticality,
    ClaimType,
    DraftBundle,
    DraftUnit,
)
from agent.graph import LangGraphAgentLoop
from agent.grounding import GroundingOutcome, GroundingStatus, assess_grounding
from agent.quality import DraftQualityPolicy
from agent.runtime import BufferedRuntimeEventSink
from agent.testing import InstrumentedCapabilityGateway, ScriptedAgentModel


def test_known_limit_valid_reference_supports_semantically_unrelated_claim():
    bundle = DraftBundle(
        schema_version="draft-bundle.v1",
        generation=1,
        units=(DraftUnit("current", "Current", "unrelated claim", 10),),
        claims=(
            Claim(
                "claim-1",
                "current",
                ClaimType.CURRENT_STATE,
                ClaimCriticality.BLOCKING,
                "orders can be cancelled by everyone",
                evidence_refs=("evidence-about-currency",),
            ),
        ),
        unknowns=(),
        markdown="unrelated claim",
    )

    findings, outcome = assess_grounding(
        bundle,
        available_evidence_refs=("evidence-about-currency",),
        supplement_count=0,
        max_supplements=1,
        has_remaining_tool_budget=True,
    )

    assert outcome == GroundingOutcome.GROUNDED
    assert findings[0].status == GroundingStatus.SUPPORTED  # known limitation
    assert findings[0].reason_code == "EVIDENCE_REFERENCE_VALID"


def test_known_limit_one_supplement_reference_is_attached_to_all_unsupported_claims():
    model = ScriptedAgentModel(
        [
            {
                "markdown": "## Current\n\nTwo unsupported statements.",
                "units": [
                    {
                        "unit_key": "current",
                        "title": "Current",
                        "order": 10,
                        "markdown": "## Current\n\nTwo unsupported statements.",
                    }
                ],
                "claims": [
                    {
                        "unit_key": "current",
                        "claim_type": "CURRENT_STATE",
                        "criticality": "BLOCKING",
                        "statement": "first unsupported statement",
                    },
                    {
                        "unit_key": "current",
                        "claim_type": "CURRENT_STATE",
                        "criticality": "BLOCKING",
                        "statement": "second unrelated statement",
                    },
                ],
            }
        ]
    )
    gateway = InstrumentedCapabilityGateway(
        repository_hits={
            "first unsupported statement": (
                RepositorySearchHit("src/first.py", 1, "only first is supported"),
            )
        }
    )
    sink = BufferedRuntimeEventSink()
    context = RunContext(
        run_id="char-supplement",
        tenant_id="eval",
        owner_id="eval",
        task_id="char",
        task_message="characterize supplement",
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
        required_coverage=(),
        advanced_loop_mode="enforce",
        max_supplements=1,
    )(context)
    draft = json.loads(result.draft_patch)

    assert len(gateway.observations) == 1
    assert len(draft["grounding_findings"]) == 2
    assert {item["status"] for item in draft["grounding_findings"]} == {"SUPPORTED"}
    assert all(item["evidence_refs"] for item in draft["grounding_findings"])
