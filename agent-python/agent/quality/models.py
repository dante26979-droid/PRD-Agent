from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class QualityScope(StrEnum):
    UNIT = "UNIT"
    FULL_DOCUMENT = "FULL_DOCUMENT"


class QualityCode(StrEnum):
    MISSING_REQUIRED_SECTION = "MISSING_REQUIRED_SECTION"
    BROKEN_TRACEABILITY = "BROKEN_TRACEABILITY"
    INCONSISTENT_TERMINOLOGY = "INCONSISTENT_TERMINOLOGY"
    RULE_CONFLICT = "RULE_CONFLICT"
    ROLE_PERMISSION_CONFLICT = "ROLE_PERMISSION_CONFLICT"
    STATE_FLOW_NOT_CLOSED = "STATE_FLOW_NOT_CLOSED"
    AMBIGUOUS_ACCEPTANCE_CRITERION = "AMBIGUOUS_ACCEPTANCE_CRITERION"
    UNRESOLVED_SOURCE_CONFLICT = "UNRESOLVED_SOURCE_CONFLICT"
    UNMARKED_UNKNOWN = "UNMARKED_UNKNOWN"
    UNSUPPORTED_CURRENT_STATE = "UNSUPPORTED_CURRENT_STATE"
    SENSITIVE_CONTENT = "SENSITIVE_CONTENT"
    CONTENT_TOO_LARGE = "CONTENT_TOO_LARGE"
    OUTLINE_SCOPE_VIOLATION = "OUTLINE_SCOPE_VIOLATION"
    IMMUTABLE_UNIT_CHANGED = "IMMUTABLE_UNIT_CHANGED"


class QualitySeverity(StrEnum):
    BLOCKING = "BLOCKING"
    IMPORTANT = "IMPORTANT"
    INFORMATIONAL = "INFORMATIONAL"


class QualityDisposition(StrEnum):
    REPAIRABLE = "REPAIRABLE"
    REQUIRES_GROUNDING = "REQUIRES_GROUNDING"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FATAL = "FATAL"


class QualityOutcome(StrEnum):
    PASSED = "QUALITY_PASSED"
    REPAIR_REQUIRED = "QUALITY_REPAIR_REQUIRED"
    NEEDS_HUMAN = "QUALITY_NEEDS_HUMAN"


@dataclass(frozen=True)
class DocumentQualityIssue:
    code: QualityCode
    severity: QualitySeverity
    disposition: QualityDisposition
    message: str
    affected_unit_keys: tuple[str, ...]
    issue_id: str = ""

    def __post_init__(self) -> None:
        if not self.message or not self.affected_unit_keys:
            raise ValueError("quality issue requires message and affected units")
        if not self.issue_id:
            raw = "\x00".join(
                (
                    self.code.value,
                    self.severity.value,
                    self.disposition.value,
                    ",".join(sorted(self.affected_unit_keys)),
                    self.message,
                )
            )
            object.__setattr__(self, "issue_id", "quality-" + hashlib.sha256(raw.encode()).hexdigest()[:20])

    def as_dict(self) -> dict[str, object]:
        return {
            "issue_id": self.issue_id,
            "code": self.code.value,
            "severity": self.severity.value,
            "disposition": self.disposition.value,
            "message": self.message,
            "affected_unit_keys": list(self.affected_unit_keys),
        }


@dataclass(frozen=True)
class DocumentQualityReport:
    scope: QualityScope
    outcome: QualityOutcome
    issues: tuple[DocumentQualityIssue, ...]
    unit_hashes: Mapping[str, str]
    schema_version: str = "quality-report.v2"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope.value,
            "outcome": self.outcome.value,
            "issues": [item.as_dict() for item in self.issues],
            "unit_hashes": dict(sorted(self.unit_hashes.items())),
        }
