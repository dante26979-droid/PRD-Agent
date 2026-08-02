from __future__ import annotations

from datetime import datetime, timezone

from prd_agent.eval.agent_trace import (
    TRACE_SCHEMA_VERSION,
    AgentLoopTrace,
    CapabilityCallTrace,
    CoverageTransitionTrace,
    GroundingFindingTrace,
    TraceCounters,
)
from prd_agent.eval.metrics import evaluate_agent_operations, evaluate_shadow_delta


def _trace(**changes):
    values = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "eval_run_id": "run-1",
        "case_id": "case-1",
        "workflow_version": "agent-runtime.v1",
        "execution_mode": "scripted_characterization",
        "deterministic_only": True,
        "status": "completed",
        "started_at": datetime(2026, 7, 31, tzinfo=timezone.utc),
        "duration_ms": 1,
        "input_hash": "sha256:a",
        "output_hash": "sha256:b",
        "result_outcome": "DRAFT_READY",
        "stop_reason": "COVERAGE_COMPLETE",
        "counters": TraceCounters(),
    }
    values.update(changes)
    return AgentLoopTrace(**values)


def _metrics(trace):
    return {item.name: item for item in evaluate_agent_operations(trace)}


def test_operational_metrics_use_physical_calls_and_unique_evidence():
    trace = _trace(
        counters=TraceCounters(
            model_attempt_count=2,
            model_physical_call_count=2,
            capability_call_count=2,
            capability_physical_call_count=2,
            evidence_count=4,
            unique_evidence_count=3,
            checkpoint_count=5,
            total_tokens=11,
            coverage_item_count=2,
            coverage_covered_count=1,
        )
    )

    metrics = _metrics(trace)

    assert metrics["model_attempt_count"].value == 2.0
    assert metrics["total_token_count"].value == 11.0
    assert metrics["evidence_yield_per_physical_call"].value == 1.5
    assert metrics["coverage_completion_rate"].value == 0.5


def test_missing_token_usage_and_empty_coverage_are_not_applicable():
    metrics = _metrics(_trace())

    assert metrics["total_token_count"].status == "not_applicable"
    assert metrics["coverage_completion_rate"].status == "not_applicable"
    assert metrics["evidence_yield_per_physical_call"].status == "not_applicable"


def test_coverage_without_fact_is_labeled_diagnostic_proxy():
    trace = _trace(
        coverage_transitions=(
            CoverageTransitionTrace(1, "a", "MISSING", "COVERED", 1, None, None, None, "OBSERVED"),
            CoverageTransitionTrace(2, "b", "MISSING", "COVERED", 1, 1, 0, 0, "OBSERVED"),
        )
    )

    metric = _metrics(trace)["coverage_without_fact_rate"]

    assert metric.value == 0.5
    assert metric.details["measurement_kind"] == "diagnostic_proxy"


def test_reference_only_support_is_diagnostic_and_not_ground_truth_accuracy():
    trace = _trace(
        grounding_findings=(
            GroundingFindingTrace(
                "claim-1", "CURRENT_STATE", "BLOCKING", "SUPPORTED",
                "EVIDENCE_REFERENCE_VALID", evidence_ref_count=1,
            ),
            GroundingFindingTrace(
                "claim-2", "CURRENT_STATE", "BLOCKING", "SUPPORTED",
                "FACT_ENTAILMENT_VALID", fact_ref_count=1,
            ),
        )
    )

    metric = _metrics(trace)["reference_only_support_rate"]

    assert metric.value == 0.5
    assert metric.details["measurement_kind"] == "diagnostic_proxy"


def test_replan_requires_a_changed_physical_action_signature():
    same = CapabilityCallTrace(1, "search", "sig-a", (), "EMPTY", 0, 0)
    changed = CapabilityCallTrace(2, "search", "sig-b", (), "EMPTY", 0, 0)

    ineffective = _trace(
        capability_calls=(same,), counters=TraceCounters(replan_count=1)
    )
    effective = _trace(
        capability_calls=(same, changed), counters=TraceCounters(replan_count=1)
    )

    assert _metrics(ineffective)["replan_effective_change_rate"].value == 0.0
    assert _metrics(effective)["replan_effective_change_rate"].value == 1.0
    assert _metrics(_trace())["replan_effective_change_rate"].status == "not_applicable"


def test_shadow_delta_uses_physical_call_counters():
    off = _trace(
        counters=TraceCounters(
            model_attempt_count=1,
            model_physical_call_count=1,
            capability_call_count=1,
            capability_physical_call_count=1,
        )
    )
    shadow = _trace(
        counters=TraceCounters(
            model_attempt_count=3,
            model_physical_call_count=3,
            capability_call_count=2,
            capability_physical_call_count=2,
        )
    )

    metrics = {item.name: item.value for item in evaluate_shadow_delta(off, shadow)}

    assert metrics["shadow_extra_model_physical_calls"] == 2.0
    assert metrics["shadow_extra_capability_physical_calls"] == 1.0


def test_duplicate_physical_signature_is_counted_but_replay_is_not():
    calls = (
        CapabilityCallTrace(1, "search", "stable", (), "SUCCEEDED", 1, 0),
        CapabilityCallTrace(2, "search", "stable", (), "SUCCEEDED", 1, 0),
        CapabilityCallTrace(
            3, "search", "stable", (), "SUCCEEDED", 1, 0,
            physical_call=False, durable_replay=True,
        ),
    )

    metric = _metrics(_trace(capability_calls=calls))[
        "resume_duplicate_physical_call_count"
    ]

    assert metric.value == 1.0
