from __future__ import annotations

from dataclasses import replace

import pytest

from agent.checkpoint import CheckpointCodec
from agent.context import RunContext
from agent.graph import LangGraphAgentLoop  # noqa: F401 - initialize graph/resume package order
from agent.resume.validator import ResumeValidator
from agent.runtime import (
    BudgetExhausted,
    BudgetVector,
    BufferedRuntimeEventSink,
    LedgerCallSpec,
    LedgerOutcomeUnknown,
    RunExecutionLedger,
)
from agent.runtime.idempotency import request_hash
from agent.v1 import agent_execution_pb2 as proto


def _context(*, sink=None, entries=(), artifacts=(), max_model_attempts=2) -> RunContext:
    return RunContext(
        run_id="run-ledger",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task",
        task_message="draft",
        workflow_version="agent-runtime.v4",
        checkpoint=b"",
        execution_ledger_version="run-ledger.v1",
        run_budget=proto.RunBudget(
            max_model_attempts=max_model_attempts,
            max_tool_calls=2,
            max_iterations=2,
            max_replans=1,
            max_supplements=1,
            max_quality_repairs=1,
            max_input_tokens=1000,
            max_output_tokens=500,
            max_elapsed_ms=60_000,
        ),
        consumed_budget=proto.ConsumedBudget(),
        ledger_entries=tuple(entries),
        resume_artifacts=tuple(artifacts),
        event_sink=sink or BufferedRuntimeEventSink(),
    )


def _spec(key="model:outline:iteration1") -> LedgerCallSpec:
    return LedgerCallSpec(
        entry_kind="MODEL",
        operation="outline",
        operation_key=key,
        request_hash=request_hash({"operation": "outline", "iteration": key}),
        reservation=BudgetVector(model_attempts=1, input_tokens=100, output_tokens=50),
        outcome_schema="outline.v1",
    )


def _validate(value):
    if not isinstance(value, dict) or not isinstance(value.get("sections"), list):
        raise ValueError("invalid outline")
    return value


def test_run_execution_ledger_orders_durability_before_and_after_physical_call():
    sink = BufferedRuntimeEventSink()
    physical_calls = 0

    def invoke():
        nonlocal physical_calls
        physical_calls += 1
        assert [item.event_type for item in sink.ledger_events] == [
            "LEDGER_RESERVED",
            "LEDGER_CALL_STARTED",
        ]
        return {"sections": ["Context"]}

    result = RunExecutionLedger(_context(sink=sink)).execute_model(_spec(), invoke, _validate)

    assert result.replayed is False
    assert physical_calls == 1
    assert [item.event_type for item in sink.ledger_events] == [
        "LEDGER_RESERVED",
        "LEDGER_CALL_STARTED",
        "LEDGER_FINISHED",
    ]
    assert len(sink.artifacts) == 1
    assert sink.ledger_events[-1].entry.output_artifact_hash == sink.artifacts[0].content_hash


def test_succeeded_entry_replays_validated_artifact_without_physical_call():
    first_sink = BufferedRuntimeEventSink()
    first = RunExecutionLedger(_context(sink=first_sink)).execute_model(
        _spec(), lambda: {"sections": ["Context"]}, _validate
    )
    durable_entry = first_sink.ledger_events[-1].entry
    replay_sink = BufferedRuntimeEventSink()
    calls = 0

    def invoke():
        nonlocal calls
        calls += 1
        return {"sections": []}

    replay = RunExecutionLedger(
        _context(sink=replay_sink, entries=(durable_entry,), artifacts=(first.artifact,))
    ).execute_model(_spec(), invoke, _validate)

    assert replay.replayed is True
    assert replay.value == {"sections": ["Context"]}
    assert calls == 0
    assert replay_sink.ledger_events == []


def test_call_started_without_outcome_is_fail_closed():
    spec = _spec()
    entry = proto.RunLedgerEntry(
        operation_key=spec.operation_key,
        entry_kind=spec.entry_kind,
        operation=spec.operation,
        request_hash=spec.request_hash,
        status="CALL_STARTED",
        reservation=spec.reservation.as_proto(),
    )
    calls = 0

    def invoke():
        nonlocal calls
        calls += 1
        return {"sections": []}

    with pytest.raises(LedgerOutcomeUnknown):
        RunExecutionLedger(_context(entries=(entry,))).execute_model(spec, invoke, _validate)
    assert calls == 0


def test_call_started_with_durable_artifact_finishes_without_replaying_provider():
    first_sink = BufferedRuntimeEventSink()
    first = RunExecutionLedger(_context(sink=first_sink)).execute_model(
        _spec(), lambda: {"sections": ["Context"]}, _validate
    )
    started = first_sink.ledger_events[1].entry
    recovery_sink = BufferedRuntimeEventSink()

    outcome = RunExecutionLedger(
        _context(sink=recovery_sink, entries=(started,), artifacts=(first.artifact,))
    ).execute_model(_spec(), lambda: pytest.fail("provider must not be replayed"), _validate)

    assert outcome.replayed is True
    assert [event.event_type for event in recovery_sink.ledger_events] == ["LEDGER_FINISHED"]


def test_active_reservation_exhausts_run_budget_before_provider_call():
    spec = _spec()
    active = proto.RunLedgerEntry(
        operation_key=spec.operation_key,
        entry_kind=spec.entry_kind,
        operation=spec.operation,
        request_hash=spec.request_hash,
        status="RESERVED",
        reservation=spec.reservation.as_proto(),
    )
    second = _spec("model:outline:iteration2")
    with pytest.raises(BudgetExhausted) as raised:
        RunExecutionLedger(_context(entries=(active,), max_model_attempts=1)).execute_model(
            second, lambda: pytest.fail("provider must not run"), _validate
        )
    assert raised.value.reason == "MODEL_ATTEMPT_BUDGET_EXHAUSTED"


def test_local_transition_is_durable_and_budgeted():
    sink = BufferedRuntimeEventSink()
    ledger = RunExecutionLedger(_context(sink=sink))
    transition = LedgerCallSpec(
        entry_kind="LOCAL_TRANSITION",
        operation="iteration",
        operation_key="transition:iteration:1",
        request_hash=request_hash({"transition": "iteration", "sequence": 1}),
        reservation=BudgetVector(iterations=1),
        outcome_schema="transition.v1",
    )
    ledger.consume_transition(transition)
    assert sink.ledger_events[0].event_type == "LEDGER_FINISHED"
    assert ledger.remaining().iterations == 1


def test_resume_accepts_ledger_owned_capability_artifact_and_evidence_ahead_of_checkpoint():
    sink = BufferedRuntimeEventSink()
    evidence = proto.EvidenceItem(
        source_type="repository",
        source_id="repo",
        locator="README.md:1",
        excerpt_hash="sha256:evidence",
        excerpt="fact",
    )
    spec = LedgerCallSpec(
        entry_kind="CAPABILITY",
        operation="search_repository",
        operation_key="capability:search_repository:action1",
        request_hash=request_hash({"query": "fact"}),
        reservation=BudgetVector(tool_calls=1),
        outcome_schema="capability-evidence.v1",
    )

    outcome = RunExecutionLedger(_context(sink=sink)).execute_capability(
        spec,
        lambda: ({"source_type": evidence.source_type, "source_id": evidence.source_id, "locator": evidence.locator, "excerpt_hash": evidence.excerpt_hash, "excerpt": evidence.excerpt},),
        lambda value: list(value),
        evidence=lambda value: tuple(proto.EvidenceItem(**item) for item in value),
    )
    call_started = sink.ledger_events[1].entry
    resumed = replace(
        _context(),
        ledger_entries=(call_started,),
        resume_artifacts=(outcome.artifact,),
        resume_evidence=(evidence,),
    )

    validated = ResumeValidator(CheckpointCodec()).hydrate(resumed)
    assert validated.state is None
