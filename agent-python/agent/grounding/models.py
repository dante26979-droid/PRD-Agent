from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class GroundingStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICTING = "CONFLICTING"
    NOT_REQUIRED = "NOT_REQUIRED"


class GroundingOutcome(StrEnum):
    GROUNDED = "GROUNDED"
    SUPPLEMENT_REQUIRED = "GROUNDING_SUPPLEMENT_REQUIRED"
    PARTIAL = "GROUNDING_PARTIAL"


@dataclass(frozen=True)
class GroundingFinding:
    claim_id: str
    status: GroundingStatus
    evidence_refs: tuple[str, ...]
    reason_code: str
    fact_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "status": self.status.value,
            "evidence_refs": list(self.evidence_refs),
            "reason_code": self.reason_code,
            "fact_ids": list(self.fact_ids),
        }
