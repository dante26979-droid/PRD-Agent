from .invariants import UnitInvariantError, UnitInvariantPolicy
from .models import (
    ConfirmedUnitContext,
    LockedOutline,
    OutlineCandidate,
    OutlineNode,
    OutlineUnit,
    RequirementBrief,
    RunOutputKind,
    RunPurpose,
    UnitCandidate,
    UnitExecutionRequest,
    UnitPatch,
    UnitResumeCursor,
    UnitRunRequest,
    UnitRunResult,
    UnitScope,
)
__all__ = [
    "ConfirmedUnitContext",
    "LockedOutline",
    "OutlineCandidate",
    "OutlineNode",
    "OutlineUnit",
    "RequirementBrief",
    "ReviewableUnitModule",
    "ReviewableUnitExecutionModule",
    "encode_run_output",
    "RunOutputKind",
    "RunPurpose",
    "UnitCandidate",
    "UnitExecutionRequest",
    "UnitInvariantError",
    "UnitInvariantPolicy",
    "UnitPatch",
    "UnitResumeCursor",
    "UnitRunRequest",
    "UnitRunResult",
    "UnitScope",
    "to_agent_result",
    "ReviewableUnitRuntime",
]


def __getattr__(name: str):
    # Keep model imports cycle-free: quality.full_review imports agent.unit.models.
    if name == "ReviewableUnitModule":
        from .runner import ReviewableUnitModule

        return ReviewableUnitModule
    if name == "ReviewableUnitExecutionModule":
        from .execution import ReviewableUnitExecutionModule

        return ReviewableUnitExecutionModule
    if name in {"encode_run_output", "to_agent_result"}:
        from .transport import encode_run_output, to_agent_result

        return {"encode_run_output": encode_run_output, "to_agent_result": to_agent_result}[name]
    if name == "ReviewableUnitRuntime":
        from .runtime import ReviewableUnitRuntime

        return ReviewableUnitRuntime
    raise AttributeError(name)
