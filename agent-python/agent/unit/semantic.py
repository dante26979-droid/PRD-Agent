from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Mapping

from agent.context import RunContext
from agent.draft import Claim, DraftBundle
from agent.grounding import GroundingModule, GroundingOutcome
from agent.knowledge import KnowledgeArtifactCodec, KnowledgeBundle
from agent.runtime import RuntimeEventSink
from agent.unit.models import UnitCandidate
from agent.v1 import agent_execution_pb2 as proto


SupplementGrounding = Callable[[Claim, KnowledgeBundle], KnowledgeBundle | None]


@dataclass
class ScopedUnitSemanticModule:
    """Production semantic boundary for one generated or revised Unit.

    The Module always extracts typed Claims and runs Grounding before Quality.
    A missing Knowledge Bundle therefore cannot silently turn a CURRENT_STATE
    claim into Supported. An optional supplement Adapter may add evidence once;
    it cannot mutate the candidate or confirmed Unit context.
    """

    context: RunContext
    sink: RuntimeEventSink
    supplement: SupplementGrounding | None = None
    max_supplements: int = 1
    _knowledge: KnowledgeBundle = field(init=False)
    _generation: int = field(init=False)

    def __post_init__(self) -> None:
        artifacts = tuple(self.context.resume_artifacts)
        knowledge_artifacts = tuple(
            item for item in artifacts if item.artifact_type == "KNOWLEDGE_BUNDLE"
        )
        if knowledge_artifacts:
            latest = max(knowledge_artifacts, key=lambda item: item.generation)
            self._knowledge = KnowledgeArtifactCodec().decode(latest)
        else:
            self._knowledge = _empty_knowledge(self.context)
        semantic_generations = (
            item.generation
            for item in artifacts
            if item.artifact_type in {"UNIT_CLAIM_SET", "UNIT_GROUNDING_REPORT"}
        )
        self._generation = max(semantic_generations, default=0)

    def evaluate(self, candidate: UnitCandidate) -> tuple[Mapping[str, object], ...]:
        self._generation += 1
        bundle = DraftBundle.build(
            run_id=self.context.run_id,
            task_id=self.context.task_id,
            task_version=max(self.context.task_version, 1),
            generation=self._generation,
            markdown=candidate.markdown,
            structured={
                "units": [
                    {
                        "unit_key": candidate.unit_key,
                        "title": candidate.title,
                        "markdown": candidate.markdown,
                        "order": candidate.ordinal,
                    }
                ],
                "claims": [item.as_dict(candidate.unit_key) for item in candidate.claims],
            }
            if candidate.claims
            else None,
        )
        self._persist(
            "UNIT_CLAIM_SET",
            "claim_set",
            {
                "schema_version": "unit-claim-set.v1",
                "scope_hash": self.context.unit_scope.scope_hash,
                "generation": self._generation,
                "claims": [item.as_dict() for item in bundle.claims],
            },
        )
        findings, outcome = self._assess(bundle, supplement_count=0)
        if outcome is GroundingOutcome.SUPPLEMENT_REQUIRED and self.supplement is not None:
            blocking = next(
                claim
                for claim, finding in zip(bundle.claims, findings, strict=True)
                if finding.status.value in {"UNSUPPORTED", "PARTIAL", "CONFLICTING"}
            )
            supplemented = self.supplement(blocking, self._knowledge)
            if supplemented is not None:
                self._knowledge = supplemented
            findings, outcome = self._assess(bundle, supplement_count=1)
        typed_findings = tuple(
            {
                **finding.as_dict(),
                "claim_type": claim.claim_type.value,
                "criticality": claim.criticality.value,
            }
            for claim, finding in zip(bundle.claims, findings, strict=True)
        )
        self._persist(
            "UNIT_GROUNDING_REPORT",
            "grounding_report",
            {
                "schema_version": "unit-grounding-report.v1",
                "scope_hash": self.context.unit_scope.scope_hash,
                "knowledge_fingerprint": self._knowledge.knowledge_fingerprint,
                "generation": self._generation,
                "outcome": outcome.value,
                "findings": list(typed_findings),
            },
        )
        return typed_findings

    def _assess(self, bundle: DraftBundle, *, supplement_count: int):
        return GroundingModule().assess(
            bundle,
            self._knowledge,
            supplement_count=supplement_count,
            max_supplements=self.max_supplements,
            has_remaining_tool_budget=self.supplement is not None,
        )

    def _persist(self, artifact_type: str, key: str, payload: Mapping[str, object]) -> None:
        content = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.sink.artifact(
            proto.RunArtifact(
                artifact_key=f"{self.context.run_id}:{key}:{self._generation}",
                artifact_type=artifact_type,
                generation=self._generation,
                request_hash=self.context.unit_scope.scope_hash,
                content_hash=hashlib.sha256(content).hexdigest(),
                content=content,
            )
        )


def _empty_knowledge(context: RunContext) -> KnowledgeBundle:
    identity = hashlib.sha256(
        "\x00".join((context.run_id, context.task_id, "empty-knowledge.v1")).encode()
    ).hexdigest()
    return KnowledgeBundle(
        schema_version="knowledge-bundle.v1",
        bundle_id="knowledge-empty-" + identity[:24],
        run_id=context.run_id,
        task_id=context.task_id,
        need_plan_id="need-not-required",
        need_context_hash=identity,
        evidence=(),
        facts=(),
        unknowns=(),
        conflicts=(),
        coverage_updates=(),
        knowledge_fingerprint=identity,
        progress_fingerprint=identity,
    )
