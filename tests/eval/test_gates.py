from __future__ import annotations

import json

import pytest

from prd_agent.eval.cli import main
from prd_agent.eval.gates import EvalGateManifest, EvalGateReport, evaluate_gate


def _manifest() -> EvalGateManifest:
    return EvalGateManifest.from_dict(
        {
            "schema_version": "agent-runtime-gate.v1",
            "dataset_hash": "dataset",
            "baseline_version": "agent-runtime.v1",
            "candidate_version": "agent-runtime.v4",
            "minimum_repetitions": 3,
            "blocking_cases": ["case-1"],
            "thresholds": [
                {"metric": "unsupported_current_state", "maximum": 0},
                {"metric": "critical_unknown_recall", "minimum": 1},
                {"metric": "shadow_extra_model_calls", "maximum": 0},
            ],
        }
    )


def _report(**overrides) -> EvalGateReport:
    value = {
        "dataset_hash": "dataset",
        "baseline_version": "agent-runtime.v1",
        "candidate_version": "agent-runtime.v4",
        "repetitions": 3,
        "metrics": {
            "unsupported_current_state": 0,
            "critical_unknown_recall": 1,
            "shadow_extra_model_calls": 0,
        },
        "failed_cases": [],
        "config_hash": "config",
        "report_hash": "report",
    }
    value.update(overrides)
    return EvalGateReport.from_dict(value)


def test_gate_passes_only_when_all_hard_metrics_and_blocking_cases_pass() -> None:
    decision = evaluate_gate(_manifest(), _report())

    assert decision.passed
    assert decision.failed_gate_codes == ()
    assert decision.manifest_hash.startswith("sha256:")


def test_gate_fails_closed_on_missing_metric_low_repetitions_and_blocking_case() -> None:
    decision = evaluate_gate(
        _manifest(),
        _report(
            repetitions=2,
            metrics={"unsupported_current_state": 0},
            failed_cases=["case-1"],
        ),
    )

    assert not decision.passed
    assert decision.failed_gate_codes == (
        "BLOCKING_CASE:case-1",
        "INSUFFICIENT_REPETITIONS",
        "MISSING_METRIC:critical_unknown_recall",
        "MISSING_METRIC:shadow_extra_model_calls",
    )


def test_gate_rejects_nan_metrics() -> None:
    with pytest.raises(ValueError, match="finite"):
        _report(metrics={"bad": float("nan")})


def test_evaluate_gate_cli_returns_nonzero_and_prints_hashes(tmp_path, capsys) -> None:
    gate_file = tmp_path / "gate.json"
    report_file = tmp_path / "report.json"
    gate_file.write_text(
        json.dumps(
            {
                "schema_version": "agent-runtime-gate.v1",
                "dataset_hash": "dataset",
                "baseline_version": "agent-runtime.v1",
                "candidate_version": "agent-runtime.v4",
                "minimum_repetitions": 3,
                "thresholds": [{"metric": "zero", "maximum": 0}],
            }
        ),
        encoding="utf-8",
    )
    report_file.write_text(
        json.dumps(
            {
                "dataset_hash": "dataset",
                "baseline_version": "agent-runtime.v1",
                "candidate_version": "agent-runtime.v4",
                "repetitions": 3,
                "metrics": {"zero": 1},
                "failed_cases": [],
                "config_hash": "config",
                "report_hash": "report",
            }
        ),
        encoding="utf-8",
    )

    assert main(["evaluate-gate", "--gate", str(gate_file), "--report", str(report_file)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["failed_gate_codes"] == ["MAXIMUM:zero"]
    assert output["manifest_hash"].startswith("sha256:")
