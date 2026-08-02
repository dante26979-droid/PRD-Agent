from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from agent.unit.models import (
    OutlineCandidate,
    UnitCandidate,
    UnitExecutionRequest,
    UnitPatch,
    UnitRunRequest,
    UnitRunResult,
)
from agent.unit.runner import ReviewableUnitModule


@dataclass(frozen=True)
class ReviewableUnitExecutionModule:
    """Deep execution boundary for exactly one frozen review purpose/scope."""

    outline_generator: Callable[[UnitRunRequest], OutlineCandidate | Mapping[str, object]] | None = None
    unit_generator: Callable[[UnitRunRequest], UnitCandidate | Mapping[str, object]] | None = None
    unit_reviser: Callable[[UnitRunRequest], UnitPatch | Mapping[str, object]] | None = None
    grounding_evaluator: Callable[[UnitCandidate], tuple[Mapping[str, object], ...]] | None = None

    def run(self, request: UnitExecutionRequest) -> UnitRunResult:
        runtime_request = UnitRunRequest(
            purpose=request.purpose,
            scope=request.scope,
            task_message=request.requirement_brief.text,
            base_candidate=request.base_candidate,
            unresolved_unknown_ids=request.unresolved_unknown_ids,
            available_fact_ids=request.available_fact_ids,
            unresolved_conflict_ids=request.unresolved_conflict_ids,
            required_content=request.requirement_brief.required_content,
            locked_outline=(
                request.locked_outline.candidate
                if request.locked_outline is not None
                else None
            ),
            resume=request.resume,
        )
        return ReviewableUnitModule(
            outline_generator=self.outline_generator,
            unit_generator=self.unit_generator,
            unit_reviser=self.unit_reviser,
            grounding_evaluator=self.grounding_evaluator,
        ).run(runtime_request)
