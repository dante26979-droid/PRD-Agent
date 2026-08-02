"""Versioned, content-safe traces for the production Agent loop.

Trace objects deliberately contain hashes, counters and low-cardinality labels
only. Drafts, prompts, evidence excerpts and raw exception messages belong to
the runtime, not to evaluation reports.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
import json
import re
from typing import Any, Mapping, TypeVar


TRACE_SCHEMA_VERSION = "agent-loop-trace.v1"
TRACE_STATUSES = frozenset({"completed", "failed"})
ATTEMPT_STATUSES = frozenset({"SUCCEEDED", "FAILED"})
CAPABILITY_STATUSES = frozenset({"SUCCEEDED", "EMPTY", "FAILED"})
COVERAGE_STATUSES = frozenset({"MISSING", "PARTIAL", "COVERED", "CONFLICTING"})
_FORBIDDEN_FAILURE_FIELDS = frozenset(
    {"stack", "traceback", "raw_message", "message", "prompt", "excerpt"}
)


def _required_text(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _non_negative(value: object, name: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a non-negative integer") from error
    if result < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return result


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


T = TypeVar("T")


def _known_values(cls: type[T], value: Mapping[str, Any]) -> dict[str, Any]:
    """Ignore future fields while keeping the v1 field set explicit."""

    names = {item.name for item in fields(cls)}
    return {key: item for key, item in value.items() if key in names}


@dataclass(frozen=True)
class ModelAttemptTrace:
    attempt_key: str
    operation: str
    prompt_version: str
    provider: str
    request_hash: str
    status: str
    output_hash: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None
    error_category: str | None = None
    physical_call: bool = True
    durable_replay: bool = False
    legacy_incomplete: bool = False

    def __post_init__(self) -> None:
        for name in ("attempt_key", "operation", "prompt_version", "provider", "request_hash"):
            _required_text(getattr(self, name), name)
        if self.status not in ATTEMPT_STATUSES:
            raise ValueError(f"unsupported model attempt status: {self.status}")
        for name in ("input_tokens", "output_tokens", "total_tokens", "latency_ms"):
            _non_negative(getattr(self, name), name, optional=True)
        if self.status == "FAILED" and not self.error_category:
            raise ValueError("failed model attempt requires error_category")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ModelAttemptTrace":
        return cls(**_known_values(cls, value))


@dataclass(frozen=True)
class CapabilityCallTrace:
    sequence: int
    capability_name: str
    action_signature: str
    target_coverage: tuple[str, ...]
    status: str
    evidence_count: int
    duration_ms: int
    physical_call: bool = True
    durable_replay: bool = False
    error_category: str | None = None

    def __post_init__(self) -> None:
        _non_negative(self.sequence, "sequence")
        _required_text(self.capability_name, "capability_name")
        _required_text(self.action_signature, "action_signature")
        if self.status not in CAPABILITY_STATUSES:
            raise ValueError(f"unsupported capability status: {self.status}")
        _non_negative(self.evidence_count, "evidence_count")
        _non_negative(self.duration_ms, "duration_ms")
        if self.status == "FAILED" and not self.error_category:
            raise ValueError("failed capability call requires error_category")

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["target_coverage"] = list(self.target_coverage)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CapabilityCallTrace":
        known = _known_values(cls, value)
        known["target_coverage"] = tuple(str(item) for item in value.get("target_coverage", []))
        return cls(**known)


@dataclass(frozen=True)
class CoverageTransitionTrace:
    iteration: int
    coverage_key: str
    before: str
    after: str
    evidence_delta: int
    fact_delta: int | None
    unknown_delta: int | None
    conflict_delta: int | None
    trigger: str

    def __post_init__(self) -> None:
        _non_negative(self.iteration, "iteration")
        _required_text(self.coverage_key, "coverage_key")
        _required_text(self.trigger, "trigger")
        if self.before not in COVERAGE_STATUSES or self.after not in COVERAGE_STATUSES:
            raise ValueError("unsupported coverage status")
        if self.before == self.after:
            raise ValueError("coverage transition must change status")
        for name in ("evidence_delta", "fact_delta", "unknown_delta", "conflict_delta"):
            _non_negative(getattr(self, name), name, optional=name != "evidence_delta")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CoverageTransitionTrace":
        return cls(**_known_values(cls, value))


@dataclass(frozen=True)
class GroundingFindingTrace:
    claim_id: str
    claim_type: str
    criticality: str
    status: str
    reason_code: str
    fact_ref_count: int = 0
    evidence_ref_count: int = 0

    def __post_init__(self) -> None:
        for name in ("claim_id", "claim_type", "criticality", "status", "reason_code"):
            _required_text(getattr(self, name), name)
        _non_negative(self.fact_ref_count, "fact_ref_count")
        _non_negative(self.evidence_ref_count, "evidence_ref_count")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GroundingFindingTrace":
        return cls(**_known_values(cls, value))


@dataclass(frozen=True)
class ArtifactTrace:
    artifact_type: str
    generation: int
    request_hash: str
    content_hash: str
    payload_bytes: int

    def __post_init__(self) -> None:
        for name in ("artifact_type", "request_hash", "content_hash"):
            _required_text(getattr(self, name), name)
        _non_negative(self.generation, "generation")
        _non_negative(self.payload_bytes, "payload_bytes")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactTrace":
        return cls(**_known_values(cls, value))


@dataclass(frozen=True)
class CheckpointTrace:
    sequence: int
    snapshot_schema: str
    status: str
    payload_bytes: int
    content_hash: str
    ack_status: str = "LOCAL"
    resume_entry: bool = False
    sequence_gap: bool = False

    def __post_init__(self) -> None:
        if _non_negative(self.sequence, "sequence") == 0:
            raise ValueError("checkpoint sequence must be positive")
        for name in ("snapshot_schema", "status", "content_hash", "ack_status"):
            _required_text(getattr(self, name), name)
        _non_negative(self.payload_bytes, "payload_bytes")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CheckpointTrace":
        return cls(**_known_values(cls, value))


@dataclass(frozen=True)
class TraceCounters:
    model_attempt_count: int = 0
    model_physical_call_count: int = 0
    model_durable_replay_count: int = 0
    capability_call_count: int = 0
    capability_physical_call_count: int = 0
    capability_durable_replay_count: int = 0
    evidence_count: int = 0
    unique_evidence_count: int = 0
    checkpoint_count: int = 0
    total_tokens: int | None = None
    replan_count: int = 0
    coverage_item_count: int = 0
    coverage_covered_count: int = 0

    def __post_init__(self) -> None:
        for item in fields(self):
            _non_negative(
                getattr(self, item.name),
                item.name,
                optional=item.name == "total_tokens",
            )
        if self.coverage_covered_count > self.coverage_item_count:
            raise ValueError("coverage_covered_count cannot exceed coverage_item_count")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TraceCounters":
        return cls(**_known_values(cls, value))


@dataclass(frozen=True)
class FailureTrace:
    category: str
    retryable: bool
    public_code: str

    def __post_init__(self) -> None:
        _required_text(self.category, "category")
        _required_text(self.public_code, "public_code")
        for value in (self.category, self.public_code):
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
                raise ValueError("failure classification must be a public low-cardinality code")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FailureTrace":
        forbidden = _FORBIDDEN_FAILURE_FIELDS.intersection(key.lower() for key in value)
        if forbidden:
            raise ValueError("failure trace contains private fields")
        return cls(**_known_values(cls, value))


@dataclass(frozen=True)
class AgentLoopTrace:
    schema_version: str
    eval_run_id: str
    case_id: str
    workflow_version: str
    execution_mode: str
    deterministic_only: bool
    status: str
    started_at: datetime
    duration_ms: int
    input_hash: str
    output_hash: str | None
    result_outcome: str | None
    stop_reason: str | None
    resume_entry_status: str | None = None
    submission_disposition: str | None = None
    incompatibility_reason_code: str | None = None
    model_attempts: tuple[ModelAttemptTrace, ...] = ()
    capability_calls: tuple[CapabilityCallTrace, ...] = ()
    coverage_transitions: tuple[CoverageTransitionTrace, ...] = ()
    grounding_findings: tuple[GroundingFindingTrace, ...] = ()
    artifacts: tuple[ArtifactTrace, ...] = ()
    checkpoints: tuple[CheckpointTrace, ...] = ()
    counters: TraceCounters = field(default_factory=TraceCounters)
    failure: FailureTrace | None = None

    def __post_init__(self) -> None:
        if self.schema_version != TRACE_SCHEMA_VERSION:
            raise ValueError(f"unsupported agent trace schema: {self.schema_version}")
        for name in (
            "eval_run_id",
            "case_id",
            "workflow_version",
            "execution_mode",
            "input_hash",
        ):
            _required_text(getattr(self, name), name)
        if self.status not in TRACE_STATUSES:
            raise ValueError(f"unsupported trace status: {self.status}")
        if not isinstance(self.started_at, datetime):
            raise ValueError("started_at must be a datetime")
        _non_negative(self.duration_ms, "duration_ms")
        if self.status == "failed" and self.failure is None:
            raise ValueError("failed trace requires failure")
        if self.status == "completed" and self.failure is not None:
            raise ValueError("completed trace cannot contain failure")
        if self.execution_mode == "remote_model" and self.deterministic_only:
            raise ValueError("remote model traces cannot be deterministic-only")
        if self.incompatibility_reason_code and not re.fullmatch(
            r"[A-Z0-9_]{1,64}", self.incompatibility_reason_code
        ):
            raise ValueError("incompatibility reason must be a low-cardinality code")
        self._validate_attempt_identity()
        self._validate_checkpoint_sequence()

    def _validate_attempt_identity(self) -> None:
        seen: dict[str, str] = {}
        for attempt in self.model_attempts:
            previous = seen.get(attempt.attempt_key)
            if previous is not None and previous != attempt.request_hash:
                raise ValueError("model attempt request hash changed for stable key")
            if previous is not None:
                raise ValueError("duplicate terminal model attempt")
            seen[attempt.attempt_key] = attempt.request_hash

    def _validate_checkpoint_sequence(self) -> None:
        previous = 0
        for item in self.checkpoints:
            if item.sequence <= previous:
                raise ValueError("checkpoint sequence must be strictly increasing")
            previous = item.sequence

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "eval_run_id": self.eval_run_id,
            "case_id": self.case_id,
            "workflow_version": self.workflow_version,
            "execution_mode": self.execution_mode,
            "deterministic_only": self.deterministic_only,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "duration_ms": self.duration_ms,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "result_outcome": self.result_outcome,
            "stop_reason": self.stop_reason,
            "resume_entry_status": self.resume_entry_status,
            "submission_disposition": self.submission_disposition,
            "incompatibility_reason_code": self.incompatibility_reason_code,
            "model_attempts": [item.as_dict() for item in self.model_attempts],
            "capability_calls": [item.as_dict() for item in self.capability_calls],
            "coverage_transitions": [item.as_dict() for item in self.coverage_transitions],
            "grounding_findings": [item.as_dict() for item in self.grounding_findings],
            "artifacts": [item.as_dict() for item in self.artifacts],
            "checkpoints": [item.as_dict() for item in self.checkpoints],
            "counters": self.counters.as_dict(),
            "failure": self.failure.as_dict() if self.failure else None,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AgentLoopTrace":
        if value.get("schema_version") != TRACE_SCHEMA_VERSION:
            raise ValueError(f"unsupported agent trace schema: {value.get('schema_version')}")
        known = _known_values(cls, value)
        try:
            known["started_at"] = datetime.fromisoformat(str(value["started_at"]))
            known["model_attempts"] = tuple(
                ModelAttemptTrace.from_dict(item) for item in value.get("model_attempts", [])
            )
            known["capability_calls"] = tuple(
                CapabilityCallTrace.from_dict(item) for item in value.get("capability_calls", [])
            )
            known["coverage_transitions"] = tuple(
                CoverageTransitionTrace.from_dict(item)
                for item in value.get("coverage_transitions", [])
            )
            known["grounding_findings"] = tuple(
                GroundingFindingTrace.from_dict(item)
                for item in value.get("grounding_findings", [])
            )
            known["artifacts"] = tuple(
                ArtifactTrace.from_dict(item) for item in value.get("artifacts", [])
            )
            known["checkpoints"] = tuple(
                CheckpointTrace.from_dict(item) for item in value.get("checkpoints", [])
            )
            counters = value.get("counters")
            if not isinstance(counters, Mapping):
                raise ValueError("trace counters are required")
            known["counters"] = TraceCounters.from_dict(counters)
            failure = value.get("failure")
            known["failure"] = (
                FailureTrace.from_dict(failure) if isinstance(failure, Mapping) else None
            )
            return cls(**known)
        except (KeyError, TypeError) as error:
            raise ValueError("agent trace is incomplete") from error


def trace_from_json(value: str) -> AgentLoopTrace:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("agent trace is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("agent trace must be an object")
    return AgentLoopTrace.from_dict(payload)
