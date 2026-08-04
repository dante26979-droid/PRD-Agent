from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Callable, Generic, TypeVar

from agent.context import RunContext
from agent.v1 import agent_execution_pb2 as proto

from .budget import BudgetState, BudgetVector
from .idempotency import outcome_artifact_key, validate_operation_key
from .outcomes import build_artifact, decode_outcome
from .reconcile import artifact_index, ledger_index

T = TypeVar("T")


class LedgerOutcomeUnknown(RuntimeError):
    pass


class LedgerTerminalFailure(RuntimeError):
    def __init__(self, category: str, retryable: bool) -> None:
        super().__init__(category or "LEDGER_CALL_FAILED")
        self.category = category
        self.retryable = retryable


@dataclass(frozen=True)
class LedgerCallSpec:
    entry_kind: str
    operation: str
    operation_key: str
    request_hash: str
    reservation: BudgetVector
    outcome_schema: str


@dataclass(frozen=True)
class LedgerOutcome(Generic[T]):
    value: T
    artifact: proto.RunArtifact
    replayed: bool
    consumption: BudgetVector


class RunExecutionLedger:
    def __init__(self, context: RunContext) -> None:
        if context.execution_ledger_version != "run-ledger.v1":
            raise ValueError("RunExecutionLedger requires run-ledger.v1")
        if context.run_budget is None or context.consumed_budget is None or context.event_sink is None:
            raise ValueError("ledger run context is incomplete")
        self._context = context
        self._sink = context.event_sink
        self._entries = ledger_index(context.ledger_entries)
        self._artifacts = artifact_index(context.resume_artifacts)
        reserved = BudgetVector()
        for item in context.ledger_entries:
            if item.status in {"RESERVED", "CALL_STARTED"}:
                reserved = reserved.add(BudgetVector.from_proto(item.reservation))
        self._budget = BudgetState(context.run_budget, BudgetVector.from_proto(context.consumed_budget), reserved)

    def execute_model(self, spec: LedgerCallSpec, invoke: Callable[[], Any], validate: Callable[[Any], T], *, consumption: Callable[[T], BudgetVector] | None = None) -> LedgerOutcome[T]:
        return self._execute(
            spec,
            invoke,
            validate,
            expected_kind="MODEL",
            artifact_type="MODEL_VALIDATED_OUTPUT",
            consumption_fn=consumption,
        )

    def execute_capability(self, spec: LedgerCallSpec, invoke: Callable[[], Any], normalize: Callable[[Any], T], *, evidence: Callable[[T], tuple[proto.EvidenceItem, ...]] | None = None, consumption: Callable[[T], BudgetVector] | None = None) -> LedgerOutcome[T]:
        return self._execute(
            spec,
            invoke,
            normalize,
            expected_kind="CAPABILITY",
            artifact_type="CAPABILITY_VALIDATED_OUTPUT",
            consumption_fn=consumption,
            evidence_fn=evidence,
        )

    def execute_derivation(
        self,
        spec: LedgerCallSpec,
        derive: Callable[[], Any],
        validate: Callable[[Any], T],
        *,
        artifact_type: str,
        consumption: Callable[[T], BudgetVector] | None = None,
    ) -> LedgerOutcome[T]:
        """Persist and replay a deterministic, non-remote derived artifact.

        Context Packs use this path so an artifact written ahead of the next
        business checkpoint is still owned by the execution ledger.
        """

        if not artifact_type or len(artifact_type) > 80:
            raise ValueError("derivation artifact_type must be bounded")
        return self._execute(
            spec,
            derive,
            validate,
            expected_kind="LOCAL_DERIVATION",
            artifact_type=artifact_type,
            consumption_fn=consumption,
        )

    def consume_transition(self, spec: LedgerCallSpec) -> None:
        self._validate_spec(spec, "LOCAL_TRANSITION")
        existing = self._entries.get(spec.operation_key)
        if existing is not None:
            self._assert_identity(existing, spec)
            if existing.status != "SUCCEEDED":
                raise LedgerOutcomeUnknown(spec.operation_key)
            return
        self._budget.consume(spec.reservation)
        entry = self._entry(spec, "SUCCEEDED", consumption=spec.reservation)
        self._sink.ledger("LEDGER_FINISHED", entry)
        self._entries[spec.operation_key] = entry

    def remaining(self) -> BudgetVector:
        return self._budget.remaining()

    def entries(self) -> tuple[proto.RunLedgerEntry, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))

    @property
    def context(self) -> RunContext:
        return self._context

    def _execute(
        self,
        spec: LedgerCallSpec,
        invoke: Callable[[], Any],
        validate: Callable[[Any], T],
        *,
        expected_kind: str,
        artifact_type: str,
        consumption_fn: Callable[[T], BudgetVector] | None,
        evidence_fn: Callable[[T], tuple[proto.EvidenceItem, ...]] | None = None,
    ) -> LedgerOutcome[T]:
        self._validate_spec(spec, expected_kind)
        existing = self._entries.get(spec.operation_key)
        if existing is not None:
            self._assert_identity(existing, spec)
            if existing.status == "SUCCEEDED":
                return self._replay(existing, spec, validate)
            if existing.status == "FAILED":
                raise LedgerTerminalFailure(existing.error_category, existing.retryable)
            if existing.status == "OUTCOME_UNKNOWN":
                raise LedgerOutcomeUnknown(spec.operation_key)
            if existing.status == "CALL_STARTED":
                key = existing.output_artifact_key or outcome_artifact_key(self._context.run_id, spec.operation_key)
                if key not in self._artifacts:
                    raise LedgerOutcomeUnknown(spec.operation_key)
                return self._finish_durable_ahead(existing, spec, validate, evidence_fn)
        else:
            self._budget.reserve(spec.reservation)
            existing = self._entry(spec, "RESERVED", reservation=spec.reservation)
            self._sink.ledger("LEDGER_RESERVED", existing)
            self._entries[spec.operation_key] = existing
        started = self._entry(spec, "CALL_STARTED", reservation=spec.reservation)
        self._sink.ledger("LEDGER_CALL_STARTED", started)
        self._entries[spec.operation_key] = started
        try:
            value = validate(invoke())
        except BaseException:
            failed = self._entry(spec, "FAILED", reservation=spec.reservation, consumption=spec.reservation, error_category="REMOTE_CALL_FAILED", retryable=False)
            self._sink.ledger("LEDGER_FINISHED", failed)
            self._budget.finish(spec.reservation, spec.reservation)
            self._entries[spec.operation_key] = failed
            raise
        artifact = build_artifact(key=outcome_artifact_key(self._context.run_id, spec.operation_key), artifact_type=artifact_type, request_hash=spec.request_hash, schema=spec.outcome_schema, value=value)
        self._sink.artifact(artifact)
        self._artifacts[artifact.artifact_key] = artifact
        evidence = evidence_fn(value) if evidence_fn is not None else ()
        if evidence:
            self._sink.evidence(evidence)
        actual = consumption_fn(value) if consumption_fn is not None else spec.reservation
        finished = self._entry(spec, "SUCCEEDED", reservation=spec.reservation, consumption=actual, output_artifact=artifact, evidence_refs=tuple(_evidence_ref(item) for item in evidence))
        self._sink.ledger("LEDGER_FINISHED", finished)
        self._budget.finish(spec.reservation, actual)
        self._entries[spec.operation_key] = finished
        return LedgerOutcome(value=value, artifact=artifact, replayed=False, consumption=actual)

    def _finish_durable_ahead(self, entry: proto.RunLedgerEntry, spec: LedgerCallSpec, validate: Callable[[Any], T], evidence_fn: Callable[[T], tuple[proto.EvidenceItem, ...]] | None) -> LedgerOutcome[T]:
        artifact = self._artifact_for(entry, spec)
        value = decode_outcome(artifact.content, spec.outcome_schema, validate)
        evidence = evidence_fn(value) if evidence_fn is not None else ()
        if evidence:
            self._sink.evidence(evidence)
        actual = BudgetVector.from_proto(entry.consumption) if entry.HasField("consumption") else spec.reservation
        if actual == BudgetVector():
            actual = spec.reservation
        finished = self._entry(spec, "SUCCEEDED", reservation=spec.reservation, consumption=actual, output_artifact=artifact, evidence_refs=tuple(_evidence_ref(item) for item in evidence))
        self._sink.ledger("LEDGER_FINISHED", finished)
        self._budget.finish(spec.reservation, actual)
        self._entries[spec.operation_key] = finished
        return LedgerOutcome(value, artifact, True, actual)

    def _replay(self, entry: proto.RunLedgerEntry, spec: LedgerCallSpec, validate: Callable[[Any], T]) -> LedgerOutcome[T]:
        artifact = self._artifact_for(entry, spec)
        value = decode_outcome(artifact.content, spec.outcome_schema, validate)
        return LedgerOutcome(value, artifact, True, BudgetVector.from_proto(entry.consumption))

    def _artifact_for(self, entry: proto.RunLedgerEntry, spec: LedgerCallSpec) -> proto.RunArtifact:
        key = entry.output_artifact_key or outcome_artifact_key(self._context.run_id, spec.operation_key)
        artifact = self._artifacts.get(key)
        if artifact is None or artifact.request_hash != spec.request_hash:
            raise LedgerOutcomeUnknown(spec.operation_key)
        if entry.output_artifact_hash and artifact.content_hash != entry.output_artifact_hash:
            raise ValueError("ledger outcome artifact hash mismatch")
        return artifact

    @staticmethod
    def _validate_spec(spec: LedgerCallSpec, expected_kind: str) -> None:
        validate_operation_key(spec.operation_key)
        if spec.entry_kind != expected_kind or not spec.operation or not spec.request_hash.startswith("sha256:"):
            raise ValueError("invalid ledger call specification")

    @staticmethod
    def _assert_identity(entry: proto.RunLedgerEntry, spec: LedgerCallSpec) -> None:
        if entry.request_hash != spec.request_hash or entry.entry_kind != spec.entry_kind or entry.operation != spec.operation:
            raise ValueError("operation_key was reused with different input")

    @staticmethod
    def _entry(spec: LedgerCallSpec, status: str, *, reservation: BudgetVector = BudgetVector(), consumption: BudgetVector = BudgetVector(), output_artifact: proto.RunArtifact | None = None, evidence_refs: tuple[str, ...] = (), error_category: str = "", retryable: bool = False) -> proto.RunLedgerEntry:
        return proto.RunLedgerEntry(operation_key=spec.operation_key, entry_kind=spec.entry_kind, operation=spec.operation, request_hash=spec.request_hash, status=status, reservation=reservation.as_proto(), consumption=consumption.as_proto(), output_artifact_key=output_artifact.artifact_key if output_artifact else "", output_artifact_hash=output_artifact.content_hash if output_artifact else "", evidence_refs=evidence_refs, error_category=error_category, retryable=retryable)


def _evidence_ref(item: proto.EvidenceItem) -> str:
    raw = "\x00".join((item.source_type, item.source_id, item.locator, item.excerpt_hash))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
