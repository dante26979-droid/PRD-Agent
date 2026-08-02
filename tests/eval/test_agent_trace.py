from __future__ import annotations

from datetime import datetime, timezone

import pytest

from prd_agent.eval.agent_trace import (
    TRACE_SCHEMA_VERSION,
    AgentLoopTrace,
    CheckpointTrace,
    CoverageTransitionTrace,
    FailureTrace,
    ModelAttemptTrace,
    TraceCounters,
    trace_from_json,
)


def _trace(**changes) -> AgentLoopTrace:
    values = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "eval_run_id": "eval-run-case-006-1",
        "case_id": "case-006",
        "workflow_version": "agent-runtime.v1",
        "execution_mode": "scripted_characterization",
        "deterministic_only": True,
        "status": "completed",
        "started_at": datetime(2026, 7, 31, tzinfo=timezone.utc),
        "duration_ms": 12,
        "input_hash": "sha256:" + "a" * 64,
        "output_hash": "sha256:" + "b" * 64,
        "result_outcome": "DRAFT_READY",
        "stop_reason": "COVERAGE_COMPLETE",
        "counters": TraceCounters(),
    }
    values.update(changes)
    return AgentLoopTrace(**values)


def test_trace_round_trip_is_deterministic_and_preserves_none():
    trace = _trace(
        coverage_transitions=(
            CoverageTransitionTrace(
                iteration=1,
                coverage_key="repository_evidence",
                before="MISSING",
                after="COVERED",
                evidence_delta=1,
                fact_delta=None,
                unknown_delta=None,
                conflict_delta=None,
                trigger="OBSERVED",
            ),
        )
    )

    encoded = trace.to_json()

    assert trace_from_json(encoded) == trace
    assert trace.to_json() == encoded
    assert '"fact_delta":null' in encoded


def test_future_fields_are_ignored_at_every_supported_level():
    value = _trace().as_dict()
    value["future_top"] = True
    value["counters"]["future_counter"] = 9

    parsed = AgentLoopTrace.from_dict(value)

    assert "future_top" not in parsed.as_dict()
    assert "future_counter" not in parsed.as_dict()["counters"]


@pytest.mark.parametrize("missing", ["eval_run_id", "case_id", "workflow_version", "input_hash"])
def test_trace_requires_identity_fields(missing):
    value = _trace().as_dict()
    value.pop(missing)

    with pytest.raises(ValueError, match="incomplete|required"):
        AgentLoopTrace.from_dict(value)


def test_unknown_schema_is_rejected():
    value = _trace().as_dict()
    value["schema_version"] = "agent-loop-trace.v999"

    with pytest.raises(ValueError, match="unsupported agent trace schema"):
        AgentLoopTrace.from_dict(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("duration_ms", -1), ("checkpoint_count", -1), ("total_tokens", -1)],
)
def test_negative_counts_are_rejected(field, value):
    if field == "duration_ms":
        with pytest.raises(ValueError, match=field):
            _trace(duration_ms=value)
    else:
        with pytest.raises(ValueError, match=field):
            TraceCounters(**{field: value})


def test_model_attempt_requires_terminal_state_and_stable_identity():
    first = ModelAttemptTrace(
        attempt_key="run:plan:1",
        operation="plan",
        prompt_version="v1",
        provider="scripted",
        request_hash="sha256:a",
        status="SUCCEEDED",
        total_tokens=2,
    )
    changed = ModelAttemptTrace(
        attempt_key="run:plan:1",
        operation="plan",
        prompt_version="v1",
        provider="scripted",
        request_hash="sha256:b",
        status="FAILED",
        error_category="MODEL_FAILURE",
    )

    with pytest.raises(ValueError, match="request hash changed"):
        _trace(model_attempts=(first, changed))
    with pytest.raises(ValueError, match="unsupported model attempt status"):
        ModelAttemptTrace(
            attempt_key="key",
            operation="plan",
            prompt_version="v1",
            provider="scripted",
            request_hash="sha256:a",
            status="PLANNED",
        )


def test_checkpoint_sequence_must_be_increasing():
    item = CheckpointTrace(1, "snapshot.v1", "OBSERVED", 10, "sha256:a")

    with pytest.raises(ValueError, match="strictly increasing"):
        _trace(checkpoints=(item, item))


def test_failure_trace_rejects_private_message_fields():
    with pytest.raises(ValueError, match="private fields"):
        FailureTrace.from_dict(
            {
                "category": "MODEL_FAILURE",
                "retryable": False,
                "public_code": "MODEL_FAILURE",
                "raw_message": "secret",
            }
        )


def test_failed_trace_requires_classified_failure():
    with pytest.raises(ValueError, match="requires failure"):
        _trace(status="failed", output_hash=None)

    trace = _trace(
        status="failed",
        output_hash=None,
        result_outcome=None,
        stop_reason=None,
        failure=FailureTrace("MODEL_FAILURE", False, "MODEL_FAILURE"),
    )
    assert trace.failure.public_code == "MODEL_FAILURE"


def test_failure_classification_and_remote_mode_cannot_bypass_report_contract():
    with pytest.raises(ValueError, match="low-cardinality"):
        FailureTrace("secret path /tmp/file", False, "MODEL_FAILURE")
    with pytest.raises(ValueError, match="cannot be deterministic"):
        _trace(execution_mode="remote_model", deterministic_only=True)
