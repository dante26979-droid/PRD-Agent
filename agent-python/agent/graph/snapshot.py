from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from agent.checkpoint import CheckpointError


LOOP_SNAPSHOT_SCHEMA_VERSION = "agent-loop-snapshot.v2"
_COMPATIBLE_SCHEMA_VERSIONS = frozenset(
    {"agent-loop-snapshot.v1", LOOP_SNAPSHOT_SCHEMA_VERSION}
)


class LoopCheckpointStatus(str, Enum):
    """Durable resume points for the controlled Agent loop."""

    INITIALIZED = "INITIALIZED"
    ACTION_VALIDATED = "ACTION_VALIDATED"
    OBSERVED = "OBSERVED"
    INVESTIGATION_FINISHED = "INVESTIGATION_FINISHED"
    DRAFTED = "DRAFTED"
    GROUNDING_SUPPLEMENT_REQUIRED = "GROUNDING_SUPPLEMENT_REQUIRED"
    GROUNDED = "GROUNDED"
    GROUNDING_PARTIAL = "GROUNDING_PARTIAL"
    QUALITY_REPAIR_REQUIRED = "QUALITY_REPAIR_REQUIRED"
    QUALITY_PASSED = "QUALITY_PASSED"
    QUALITY_NEEDS_HUMAN = "QUALITY_NEEDS_HUMAN"
    CONFIRMATION_UNITS_BUILT = "CONFIRMATION_UNITS_BUILT"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"


@dataclass(frozen=True)
class LoopSnapshot:
    status: LoopCheckpointStatus
    state: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "snapshot_schema_version": LOOP_SNAPSHOT_SCHEMA_VERSION,
            "status": self.status.value,
            # Kept as a read-only compatibility alias for pre-snapshot tooling.
            # Recovery never consults it when the versioned status is present.
            "stage": self.status.value,
            "snapshot": dict(self.state),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "LoopSnapshot":
        schema_version = payload.get("snapshot_schema_version")
        if schema_version is None:
            return cls._from_legacy_payload(payload)
        if schema_version not in _COMPATIBLE_SCHEMA_VERSIONS:
            raise CheckpointError("loop snapshot schema is incompatible")
        raw_state = payload.get("snapshot")
        if not isinstance(raw_state, dict):
            raise CheckpointError("loop checkpoint snapshot must be an object")
        status = _parse_status(payload.get("status"))
        return cls._validated(status, raw_state)

    @classmethod
    def _from_legacy_payload(cls, payload: Mapping[str, Any]) -> "LoopSnapshot":
        """Resume checkpoints emitted before versioned loop snapshots existed."""

        raw_state = payload.get("graph_state")
        if not isinstance(raw_state, dict):
            raise CheckpointError("loop checkpoint snapshot is missing")
        status = _parse_status(payload.get("stage"))
        return cls._validated(status, raw_state)

    @classmethod
    def _validated(
        cls,
        status: LoopCheckpointStatus,
        raw_state: Mapping[str, Any],
    ) -> "LoopSnapshot":
        state = dict(raw_state)
        if status is LoopCheckpointStatus.ACTION_VALIDATED:
            if not isinstance(state.get("pending_action"), dict) or not state.get(
                "action_signature"
            ):
                raise CheckpointError(
                    "ACTION_VALIDATED snapshot requires a validated pending action"
                )
        elif status is LoopCheckpointStatus.READY_TO_SUBMIT:
            markdown = state.get("candidate_markdown")
            artifact_key = state.get("draft_artifact_key")
            artifact_hash = state.get("draft_artifact_hash")
            has_inline_draft = isinstance(markdown, str) and bool(markdown.strip())
            has_external_draft = bool(artifact_key) and bool(artifact_hash)
            if not has_inline_draft and not has_external_draft:
                raise CheckpointError(
                    "READY_TO_SUBMIT snapshot requires candidate markdown "
                    "or a durable draft artifact"
                )
            if not state.get("draft_hash"):
                raise CheckpointError("READY_TO_SUBMIT snapshot requires draft hash")

        # The envelope status is authoritative. Stale in-memory node fields must
        # never redirect recovery to a different side-effect boundary.
        state["status"] = status.value
        state["phase"] = status.value
        return cls(status=status, state=state)


def _parse_status(value: object) -> LoopCheckpointStatus:
    try:
        return LoopCheckpointStatus(str(value))
    except ValueError as error:
        raise CheckpointError(f"unsupported loop checkpoint status: {value}") from error
