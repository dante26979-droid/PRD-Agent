from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from prd_agent.hashing import sha256_json


@dataclass(frozen=True)
class GateThreshold:
    metric: str
    minimum: float | None = None
    maximum: float | None = None
    required: bool = True

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "GateThreshold":
        metric = str(value.get("metric", "")).strip()
        if not metric:
            raise ValueError("gate threshold requires metric")
        minimum = _optional_float(value.get("minimum"))
        maximum = _optional_float(value.get("maximum"))
        if minimum is None and maximum is None:
            raise ValueError("gate threshold requires a minimum or maximum")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("gate threshold minimum exceeds maximum")
        return cls(metric, minimum, maximum, bool(value.get("required", True)))


@dataclass(frozen=True)
class EvalGateManifest:
    dataset_hash: str
    baseline_version: str
    candidate_version: str
    minimum_repetitions: int
    thresholds: tuple[GateThreshold, ...]
    blocking_cases: tuple[str, ...]
    schema_version: str = "agent-runtime-gate.v1"

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "EvalGateManifest":
        if value.get("schema_version") != "agent-runtime-gate.v1":
            raise ValueError("unsupported eval gate schema")
        dataset_hash = str(value.get("dataset_hash", "")).strip()
        baseline = str(value.get("baseline_version", "")).strip()
        candidate = str(value.get("candidate_version", "")).strip()
        repetitions = int(value.get("minimum_repetitions", 0))
        if not dataset_hash or not baseline or not candidate or repetitions < 1:
            raise ValueError("eval gate requires identities and repetitions")
        raw_thresholds = value.get("thresholds")
        if not isinstance(raw_thresholds, list) or not raw_thresholds:
            raise ValueError("eval gate requires thresholds")
        thresholds = tuple(
            GateThreshold.from_dict(item)
            for item in raw_thresholds
            if isinstance(item, Mapping)
        )
        if len(thresholds) != len(raw_thresholds):
            raise ValueError("eval gate thresholds must be objects")
        names = [item.metric for item in thresholds]
        if len(names) != len(set(names)):
            raise ValueError("eval gate metrics must be unique")
        return cls(
            dataset_hash=dataset_hash,
            baseline_version=baseline,
            candidate_version=candidate,
            minimum_repetitions=repetitions,
            thresholds=thresholds,
            blocking_cases=tuple(sorted(set(str(item) for item in value.get("blocking_cases", [])))),
        )

    @property
    def manifest_hash(self) -> str:
        return sha256_json(
            {
                "schema_version": self.schema_version,
                "dataset_hash": self.dataset_hash,
                "baseline_version": self.baseline_version,
                "candidate_version": self.candidate_version,
                "minimum_repetitions": self.minimum_repetitions,
                "thresholds": [item.__dict__ for item in self.thresholds],
                "blocking_cases": list(self.blocking_cases),
            }
        )


@dataclass(frozen=True)
class EvalGateReport:
    dataset_hash: str
    baseline_version: str
    candidate_version: str
    repetitions: int
    metrics: Mapping[str, float]
    failed_cases: tuple[str, ...]
    config_hash: str
    report_hash: str

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "EvalGateReport":
        raw_metrics = value.get("metrics")
        if not isinstance(raw_metrics, Mapping):
            raise ValueError("eval gate report requires metrics")
        metrics = {str(key): float(metric) for key, metric in raw_metrics.items()}
        if any(not math.isfinite(metric) for metric in metrics.values()):
            raise ValueError("eval gate metrics must be finite")
        report = cls(
            dataset_hash=str(value.get("dataset_hash", "")),
            baseline_version=str(value.get("baseline_version", "")),
            candidate_version=str(value.get("candidate_version", "")),
            repetitions=int(value.get("repetitions", 0)),
            metrics=metrics,
            failed_cases=tuple(sorted(set(str(item) for item in value.get("failed_cases", [])))),
            config_hash=str(value.get("config_hash", "")),
            report_hash=str(value.get("report_hash", "")),
        )
        if not report.config_hash or not report.report_hash:
            raise ValueError("eval gate report requires config and report hashes")
        return report


@dataclass(frozen=True)
class EvalGateDecision:
    passed: bool
    failed_gate_codes: tuple[str, ...]
    manifest_hash: str
    report_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "failed_gate_codes": list(self.failed_gate_codes),
            "manifest_hash": self.manifest_hash,
            "report_hash": self.report_hash,
        }


def evaluate_gate(
    manifest: EvalGateManifest,
    report: EvalGateReport,
) -> EvalGateDecision:
    failures: list[str] = []
    if report.dataset_hash != manifest.dataset_hash:
        failures.append("DATASET_HASH_MISMATCH")
    if (
        report.baseline_version != manifest.baseline_version
        or report.candidate_version != manifest.candidate_version
    ):
        failures.append("WORKFLOW_VERSION_MISMATCH")
    if report.repetitions < manifest.minimum_repetitions:
        failures.append("INSUFFICIENT_REPETITIONS")
    failed_cases = set(report.failed_cases)
    failures.extend(
        "BLOCKING_CASE:" + item
        for item in manifest.blocking_cases
        if item in failed_cases
    )
    for threshold in manifest.thresholds:
        metric = report.metrics.get(threshold.metric)
        if metric is None:
            if threshold.required:
                failures.append("MISSING_METRIC:" + threshold.metric)
            continue
        if threshold.minimum is not None and metric < threshold.minimum:
            failures.append("MINIMUM:" + threshold.metric)
        if threshold.maximum is not None and metric > threshold.maximum:
            failures.append("MAXIMUM:" + threshold.metric)
    failures = sorted(set(failures))
    return EvalGateDecision(
        passed=not failures,
        failed_gate_codes=tuple(failures),
        manifest_hash=manifest.manifest_hash,
        report_hash=report.report_hash,
    )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("gate threshold must be finite")
    return result
