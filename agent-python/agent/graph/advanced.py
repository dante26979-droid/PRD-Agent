from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Callable

from langgraph.graph import END, START, StateGraph

from agent.confirmation import build_confirmation_units
from agent.context import RunContext
from agent.draft import Claim, DraftBundle
from agent.grounding import (
    GroundingOutcome,
    GroundingStatus,
    assess_grounding,
    materialize_unknowns,
)
from agent.quality import QualityOutcome, check_advanced_quality
from agent.runtime import RuntimeEventSink
from agent.v1 import agent_execution_pb2 as proto

from .snapshot import LoopCheckpointStatus
from .state import AgentState


@dataclass(frozen=True)
class AdvancedLoopRunner:
    """Artifact-backed grounding, quality and confirmation LangGraph."""

    context: RunContext
    sink: RuntimeEventSink
    emit_checkpoint: Callable[[AgentState, LoopCheckpointStatus], int]
    generate_draft: Callable[[AgentState], tuple[str, int]]
    repair_draft: Callable[[AgentState, tuple[dict[str, object], ...]], tuple[str, int]]
    supplement: Callable[[str], tuple[proto.EvidenceItem, ...]] | None
    max_supplements: int = 1
    max_repairs: int = 1

    def invoke(self, state: AgentState) -> AgentState:
        initial = dict(state)
        if initial.get("draft_artifact_key"):
            initial["draft_bundle"] = self._load_bundle(
                str(initial["draft_artifact_key"]),
                str(initial.get("draft_artifact_hash") or ""),
            ).as_dict()
        return self._build_graph().invoke(initial)

    def _build_graph(self):
        builder = StateGraph(AgentState)

        def route_resume(state: AgentState) -> str:
            status = str(state.get("status"))
            if status == LoopCheckpointStatus.INVESTIGATION_FINISHED.value:
                return "draft"
            if status == LoopCheckpointStatus.DRAFTED.value:
                return "ground"
            if status == LoopCheckpointStatus.GROUNDING_SUPPLEMENT_REQUIRED.value:
                return "supplement"
            if status in {
                LoopCheckpointStatus.GROUNDED.value,
                LoopCheckpointStatus.GROUNDING_PARTIAL.value,
            }:
                return "quality"
            if status == LoopCheckpointStatus.QUALITY_REPAIR_REQUIRED.value:
                return "repair"
            if status in {
                LoopCheckpointStatus.QUALITY_PASSED.value,
                LoopCheckpointStatus.QUALITY_NEEDS_HUMAN.value,
            }:
                return "confirmation"
            if status == LoopCheckpointStatus.CONFIRMATION_UNITS_BUILT.value:
                return "done"
            raise ValueError(f"unsupported advanced loop status: {status}")

        def draft(state: AgentState):
            markdown = state.get("candidate_markdown")
            tokens = 0
            if not isinstance(markdown, str) or not markdown.strip():
                markdown, tokens = self.generate_draft(state)
            generation = int(state.get("draft_generation", 0)) + 1
            bundle = DraftBundle.build(
                run_id=self.context.run_id,
                task_id=self.context.task_id,
                task_version=max(self.context.task_version, 1),
                generation=generation,
                markdown=markdown,
                structured=(
                    state.get("draft_structured")
                    if isinstance(state.get("draft_structured"), dict)
                    else None
                ),
            )
            bundle = self._apply_revision_scope(bundle)
            artifact = self._save_bundle(bundle)
            updated: AgentState = {
                **state,
                "candidate_markdown": bundle.markdown,
                "draft_generation": generation,
                "draft_bundle": bundle.as_dict(),
                "draft_artifact_key": artifact.artifact_key,
                "draft_artifact_hash": artifact.content_hash,
                "token_usage": int(state.get("token_usage", 0)) + tokens,
            }
            return self._checkpoint(updated, LoopCheckpointStatus.DRAFTED)

        def ground(state: AgentState):
            bundle = self._bundle(state)
            findings, outcome = assess_grounding(
                bundle,
                available_evidence_refs=state.get("evidence_refs", ()),
                supplement_count=int(state.get("supplement_count", 0)),
                max_supplements=self.max_supplements,
                has_remaining_tool_budget=(
                    self.supplement is not None
                    and int(state.get("tool_call_count", 0)) < 8
                ),
            )
            report = {
                "schema_version": "grounding-report.v1",
                "draft_generation": bundle.generation,
                "outcome": outcome.value,
                "findings": [item.as_dict() for item in findings],
            }
            report_artifact = self._save_json_artifact(
                artifact_type="GROUNDING_REPORT",
                generation=bundle.generation,
                value=report,
            )
            updated: AgentState = {
                **state,
                "grounding_findings": report["findings"],
                "grounding_outcome": outcome.value,
                "grounding_artifact_key": report_artifact.artifact_key,
            }
            if outcome == GroundingOutcome.PARTIAL:
                grounded_bundle = materialize_unknowns(bundle, findings)
                if grounded_bundle.markdown != bundle.markdown:
                    grounded_bundle = replace(
                        grounded_bundle,
                        generation=bundle.generation + 1,
                    )
                    draft_artifact = self._save_bundle(grounded_bundle)
                    updated.update(
                        {
                            "candidate_markdown": grounded_bundle.markdown,
                            "draft_generation": grounded_bundle.generation,
                            "draft_bundle": grounded_bundle.as_dict(),
                            "draft_artifact_key": draft_artifact.artifact_key,
                            "draft_artifact_hash": draft_artifact.content_hash,
                        }
                    )
            status = LoopCheckpointStatus(outcome.value)
            return self._checkpoint(updated, status)

        def supplement(state: AgentState):
            if self.supplement is None:
                return self._checkpoint(
                    {**state, "grounding_outcome": GroundingOutcome.PARTIAL.value},
                    LoopCheckpointStatus.GROUNDING_PARTIAL,
                )
            bundle = self._bundle(state)
            unsupported = {
                str(item["claim_id"])
                for item in state.get("grounding_findings", ())
                if item.get("status")
                in {
                    GroundingStatus.UNSUPPORTED.value,
                    GroundingStatus.PARTIAL.value,
                    GroundingStatus.CONFLICTING.value,
                }
            }
            target = next(
                (claim for claim in bundle.claims if claim.claim_id in unsupported),
                None,
            )
            if target is None:
                return self._checkpoint(state, LoopCheckpointStatus.GROUNDED)
            items = self.supplement(target.statement)
            if items:
                self.sink.evidence(items)
            references = list(state.get("evidence_refs", ()))
            observations = list(state.get("observations", ()))
            for item in items:
                reference = _evidence_reference(item)
                if reference not in references:
                    references.append(reference)
                    observations.append(
                        {
                            "source_type": item.source_type,
                            "source_id": item.source_id,
                            "locator": item.locator,
                            "excerpt_hash": item.excerpt_hash,
                            "summary": item.excerpt[:1000],
                        }
                    )
            claims = tuple(
                replace(
                    claim,
                    evidence_refs=tuple(
                        dict.fromkeys((*claim.evidence_refs, *references))
                    ),
                )
                if claim.claim_id in unsupported
                else claim
                for claim in bundle.claims
            )
            revised = replace(
                bundle,
                generation=bundle.generation + 1,
                claims=claims,
            )
            artifact = self._save_bundle(revised)
            updated: AgentState = {
                **state,
                "draft_generation": revised.generation,
                "draft_bundle": revised.as_dict(),
                "draft_artifact_key": artifact.artifact_key,
                "draft_artifact_hash": artifact.content_hash,
                "evidence_refs": references,
                "observations": observations,
                "supplement_count": int(state.get("supplement_count", 0)) + 1,
                "tool_call_count": int(state.get("tool_call_count", 0)) + 1,
            }
            return self._checkpoint(updated, LoopCheckpointStatus.DRAFTED)

        def quality(state: AgentState):
            bundle = self._bundle(state)
            outcome_value = str(
                state.get("grounding_outcome", GroundingOutcome.GROUNDED.value)
            )
            findings, outcome = check_advanced_quality(
                bundle,
                grounding_outcome=GroundingOutcome(outcome_value),
                repair_count=int(state.get("repair_count", 0)),
                max_repairs=self.max_repairs,
            )
            report = {
                "schema_version": "quality-report.v1",
                "draft_generation": bundle.generation,
                "outcome": outcome.value,
                "issues": [item.as_dict() for item in findings],
            }
            artifact = self._save_json_artifact(
                artifact_type="QUALITY_REPORT",
                generation=bundle.generation,
                value=report,
            )
            return self._checkpoint(
                {
                    **state,
                    "quality_issues": report["issues"],
                    "quality_outcome": outcome.value,
                    "quality_artifact_key": artifact.artifact_key,
                },
                LoopCheckpointStatus(outcome.value),
            )

        def repair(state: AgentState):
            issues = tuple(state.get("quality_issues", ()))
            markdown, tokens = self.repair_draft(state, issues)
            updated: AgentState = {
                **state,
                "candidate_markdown": markdown,
                "repair_count": int(state.get("repair_count", 0)) + 1,
                "token_usage": int(state.get("token_usage", 0)) + tokens,
                "draft_artifact_key": None,
                "draft_artifact_hash": None,
            }
            return updated

        def confirmation(state: AgentState):
            bundle = self._bundle(state)
            immutable = tuple(state.get("immutable_unit_keys", ()))
            units = build_confirmation_units(
                bundle,
                immutable_unit_keys=immutable,
            )
            artifact = self._save_json_artifact(
                artifact_type="CONFIRMATION_UNITS",
                generation=bundle.generation,
                value={
                    "schema_version": "confirmation-units.v1",
                    "draft_generation": bundle.generation,
                    "units": list(units),
                },
            )
            return self._checkpoint(
                {
                    **state,
                    "confirmation_units": list(units),
                    "confirmation_artifact_key": artifact.artifact_key,
                },
                LoopCheckpointStatus.CONFIRMATION_UNITS_BUILT,
            )

        builder.add_node("resume", lambda state: {})
        builder.add_node("draft", draft)
        builder.add_node("ground", ground)
        builder.add_node("supplement", supplement)
        builder.add_node("quality", quality)
        builder.add_node("repair", repair)
        builder.add_node("confirmation", confirmation)
        builder.add_edge(START, "resume")
        builder.add_conditional_edges(
            "resume",
            route_resume,
            {
                "draft": "draft",
                "ground": "ground",
                "supplement": "supplement",
                "quality": "quality",
                "repair": "repair",
                "confirmation": "confirmation",
                "done": END,
            },
        )
        builder.add_edge("draft", "ground")
        builder.add_conditional_edges(
            "ground",
            lambda state: (
                "supplement"
                if state.get("status")
                == LoopCheckpointStatus.GROUNDING_SUPPLEMENT_REQUIRED.value
                else "quality"
            ),
            {"supplement": "supplement", "quality": "quality"},
        )
        builder.add_edge("supplement", "ground")
        builder.add_conditional_edges(
            "quality",
            lambda state: (
                "repair"
                if state.get("status")
                == LoopCheckpointStatus.QUALITY_REPAIR_REQUIRED.value
                else "confirmation"
            ),
            {"repair": "repair", "confirmation": "confirmation"},
        )
        builder.add_edge("repair", "draft")
        builder.add_edge("confirmation", END)
        return builder.compile()

    def _checkpoint(
        self,
        state: AgentState,
        status: LoopCheckpointStatus,
    ) -> AgentState:
        sequence = self.emit_checkpoint(state, status)
        return {
            **state,
            "checkpoint_sequence": sequence,
            "status": status.value,
            "phase": status.value,
        }

    def _bundle(self, state: AgentState) -> DraftBundle:
        raw = state.get("draft_bundle")
        if isinstance(raw, dict):
            return DraftBundle.from_dict(raw)
        key = str(state.get("draft_artifact_key") or "")
        digest = str(state.get("draft_artifact_hash") or "")
        if not key:
            raise ValueError("advanced loop requires a draft artifact")
        return self._load_bundle(key, digest)

    def _load_bundle(self, key: str, expected_hash: str) -> DraftBundle:
        matches = [
            item for item in self.context.resume_artifacts if item.artifact_key == key
        ]
        if not matches:
            raise ValueError(f"required run artifact is unavailable: {key}")
        artifact = matches[-1]
        actual_hash = hashlib.sha256(artifact.content).hexdigest()
        if expected_hash and actual_hash != expected_hash:
            raise ValueError("run artifact content hash mismatch")
        raw = json.loads(artifact.content.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("draft artifact must contain a JSON object")
        return DraftBundle.from_dict(raw)

    def _save_bundle(self, bundle: DraftBundle) -> proto.RunArtifact:
        return self._save_json_artifact(
            artifact_type="DRAFT_BUNDLE",
            generation=bundle.generation,
            value=bundle.as_dict(),
        )

    def _apply_revision_scope(self, candidate: DraftBundle) -> DraftBundle:
        scope = self.context.revision_scope
        receipt = self.context.resume_draft
        if scope is None or receipt is None or not receipt.content:
            return candidate
        raw = json.loads(receipt.content.decode("utf-8"))
        raw_units = raw.get("confirmation_units")
        if not isinstance(raw_units, list):
            raise ValueError("revision base draft has no confirmation units")
        reopened = frozenset(scope.reopened_unit_keys)
        immutable = frozenset(scope.immutable_unit_keys)
        if not reopened or reopened & immutable:
            raise ValueError("revision scope is inconsistent")
        candidate_by_key = {item.unit_key: item for item in candidate.units}
        merged = []
        seen = set()
        for raw_unit in raw_units:
            if not isinstance(raw_unit, dict):
                raise ValueError("revision base unit is invalid")
            key = str(raw_unit.get("unit_key", ""))
            if not key or key in seen:
                raise ValueError("revision base unit identity is invalid")
            seen.add(key)
            markdown = str(raw_unit.get("markdown", ""))
            title = str(raw_unit.get("title", ""))
            order = int(raw_unit.get("order", 0))
            depends_on = list(raw_unit.get("depends_on", ()))
            if key in reopened:
                revised = candidate_by_key.get(key)
                if revised is not None:
                    markdown = revised.markdown
                    title = revised.title
                feedback = scope.user_feedback.strip()
                if feedback and feedback not in markdown:
                    markdown = markdown.rstrip() + f"\n\n修订反馈：{feedback}"
            elif key not in immutable:
                # A scoped revision may not silently mutate or introduce a
                # unit that the control plane did not reopen.
                immutable = frozenset((*immutable, key))
            merged.append(
                {
                    "unit_key": key,
                    "title": title,
                    "order": order,
                    "markdown": markdown,
                    "depends_on": depends_on,
                }
            )
        if not reopened.issubset(seen):
            raise ValueError("reopened unit is absent from the base draft")
        revised = DraftBundle.build(
            run_id=self.context.run_id,
            task_id=self.context.task_id,
            task_version=max(self.context.task_version, 1),
            generation=candidate.generation,
            markdown="\n\n".join(str(item["markdown"]) for item in merged),
            structured={"units": merged},
        )
        return revised

    def _save_json_artifact(
        self,
        *,
        artifact_type: str,
        generation: int,
        value: dict[str, object],
    ) -> proto.RunArtifact:
        content = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        content_hash = hashlib.sha256(content).hexdigest()
        artifact = proto.RunArtifact(
            artifact_key=(
                f"{self.context.run_id}:{artifact_type.lower()}:{generation}"
            ),
            artifact_type=artifact_type,
            generation=generation,
            request_hash=hashlib.sha256(
                f"{artifact_type}\0{generation}\0{content_hash}".encode("utf-8")
            ).hexdigest(),
            content_hash=content_hash,
            content=content,
        )
        self.sink.artifact(artifact)
        return artifact


def _evidence_reference(item: proto.EvidenceItem) -> str:
    return "sha256:" + hashlib.sha256(
        "\x00".join(
            (item.source_type, item.source_id, item.locator, item.excerpt_hash)
        ).encode("utf-8")
    ).hexdigest()
