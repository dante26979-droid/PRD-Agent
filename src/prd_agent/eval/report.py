"""Markdown and JSON report generation for baseline runs."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from statistics import mean
from typing import Any

from .metrics import (
    MetricResult,
    evaluate_agent_operations,
    evaluate_case,
    evaluate_information_need,
    expected_information_need_requiredness,
)
from .models import BaselineConfig, BaselineRun, EvalDataset


@dataclass(frozen=True)
class EvaluationReport:
    dataset_version: str
    repository_id: str
    repository_commit: str
    config_id: str
    case_count: int
    trial_count: int
    completed_runs: int
    failed_runs: int
    metric_summary: dict[str, dict[str, Any]]
    information_need_confusion_matrix: dict[str, dict[str, int]]
    failures: tuple[dict[str, Any], ...]
    run_details: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset_version": self.dataset_version,
            "repository_id": self.repository_id,
            "repository_commit": self.repository_commit,
            "config_id": self.config_id,
            "case_count": self.case_count,
            "trial_count": self.trial_count,
            "completed_runs": self.completed_runs,
            "failed_runs": self.failed_runs,
            "metric_summary": self.metric_summary,
            "information_need_confusion_matrix": self.information_need_confusion_matrix,
            "failures": list(self.failures),
            "run_details": list(self.run_details),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def to_markdown(self) -> str:
        lines = [
            "# M0 PRD Agent Evaluation Report",
            "",
            f"- Dataset: `{self.dataset_version}`",
            f"- Repository: `{self.repository_id}`",
            f"- Commit: `{self.repository_commit}`",
            f"- Config: `{self.config_id}`",
            f"- Cases: `{self.case_count}`",
            f"- Trials: `{self.trial_count}`",
            f"- Completed runs: `{self.completed_runs}`",
            f"- Failed runs: `{self.failed_runs}`",
            "",
            "## Metric Summary",
            "",
            "| Metric | Mean | Min | Max | Status |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        for name, summary in sorted(self.metric_summary.items()):
            lines.append(
                f"| {name} | {summary.get('mean', '—')} | {summary.get('min', '—')} | "
                f"{summary.get('max', '—')} | {summary['status']} |"
            )
        labels = ("NONE", "OPTIONAL", "REQUIRED")
        lines.extend(
            [
                "",
                "## Information Need Confusion Matrix",
                "",
                "| Expected \\ Actual | NONE | OPTIONAL | REQUIRED |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for expected in labels:
            row = self.information_need_confusion_matrix[expected]
            lines.append(
                f"| {expected} | {row['NONE']} | {row['OPTIONAL']} | {row['REQUIRED']} |"
            )
        lines.extend(["", "## Run Details", "", "| Run | Case | Trial | Status | Input Hash |", "| --- | --- | ---: | --- | --- |"])
        for run in self.run_details:
            lines.append(
                f"| {run['eval_run_id']} | {run['case_id']} | {run['trial_no']} | "
                f"{run['status']} | {run['input_hash']} |"
            )
        if self.failures:
            lines.extend(["", "## Failures", ""])
            for failure in self.failures:
                lines.append(
                    f"- `{failure['eval_run_id']}` ({failure['case_id']}): "
                    f"{failure['error_category']}"
                )
        return "\n".join(lines) + "\n"


def _public_error_category(run: BaselineRun) -> str:
    trace = run.metadata.get("trace") if isinstance(run.metadata, dict) else None
    if isinstance(trace, dict):
        failure = trace.get("failure")
        if isinstance(failure, dict) and failure.get("category"):
            candidate = str(failure["category"])
            return (
                candidate
                if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", candidate)
                else "UNKNOWN_ERROR"
            )
    name = str(run.error or "UNKNOWN_ERROR").split(":", 1)[0]
    if name == "TimeoutError":
        return "timeout"
    return name if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) else "UNKNOWN_ERROR"


def _safe_run_detail(run: BaselineRun) -> dict[str, Any]:
    metadata = run.metadata if isinstance(run.metadata, dict) else {}
    trace = metadata.get("trace")
    counters = trace.get("counters", {}) if isinstance(trace, dict) else {}
    return {
        "eval_run_id": run.eval_run_id,
        "case_id": run.case_id,
        "trial_no": run.trial_no,
        "status": run.status,
        "input_hash": run.input_hash,
        "output_hash": run.output_hash,
        "duration_ms": run.duration_ms,
        "model_id": run.model_id,
        "prompt_version": run.prompt_version,
        "measurement_mode": metadata.get("measurement_mode", "legacy_baseline"),
        "deterministic_only": metadata.get("deterministic_only"),
        "workflow_version": metadata.get("workflow_version"),
        "route": metadata.get("route"),
        "counters": dict(counters) if isinstance(counters, dict) else {},
        "error_category": _public_error_category(run) if run.status != "completed" else None,
    }


def _summarize(results: list[MetricResult]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[MetricResult]] = {}
    for result in results:
        grouped.setdefault(result.name, []).append(result)
    summary: dict[str, dict[str, Any]] = {}
    for name, items in grouped.items():
        measured = [item.value for item in items if item.value is not None]
        statuses = {item.status for item in items}
        summary[name] = {
            "mean": round(mean(measured), 4) if measured else None,
            "min": min(measured) if measured else None,
            "max": max(measured) if measured else None,
            "status": "not_applicable" if statuses == {"not_applicable"} else "measured",
        }
    return summary


def build_report(
    dataset: EvalDataset, config: BaselineConfig, runs: list[BaselineRun]
) -> EvaluationReport:
    completed = [run for run in runs if run.status == "completed" and run.output is not None]
    metric_results: list[MetricResult] = []
    labels = ("NONE", "OPTIONAL", "REQUIRED")
    confusion = {
        expected: {actual: 0 for actual in labels} for expected in labels
    }
    for run in completed:
        case = next(case for case in dataset.cases if case.case_id == run.case_id)
        metric_results.extend(evaluate_case(case, run.output or ""))
        for name in (
            "coverage_completion_rate",
            "tool_call_count",
            "duplicate_action_rate",
            "replan_count",
            "no_progress_termination_rate",
        ):
            if name in run.metadata:
                metric_results.append(
                    MetricResult(name=name, value=float(run.metadata[name]))
                )
        trace = run.metadata.get("trace")
        if isinstance(trace, dict) and trace.get("schema_version"):
            metric_results.extend(evaluate_agent_operations(trace))
            metric_results.extend(evaluate_information_need(case, trace))
            raw_need = trace.get("information_need")
            if isinstance(raw_need, dict):
                actual = str(raw_need.get("requiredness", ""))
                if actual in labels:
                    expected = expected_information_need_requiredness(case)
                    confusion[expected][actual] += 1
    failures = tuple(
        {
            "eval_run_id": run.eval_run_id,
            "case_id": run.case_id,
            "trial_no": run.trial_no,
            "error_category": _public_error_category(run),
        }
        for run in runs
        if run.status != "completed"
    )
    summary = _summarize(metric_results)
    for label in labels:
        key = label.lower()
        true_positive = confusion[label][label]
        predicted = sum(confusion[expected][label] for expected in labels)
        expected_count = sum(confusion[label].values())
        for metric_name, numerator, denominator in (
            (f"information_need_{key}_precision", true_positive, predicted),
            (f"information_need_{key}_recall", true_positive, expected_count),
        ):
            value = round(numerator / denominator, 4) if denominator else None
            summary[metric_name] = {
                "mean": value,
                "min": value,
                "max": value,
                "status": "measured" if denominator else "not_applicable",
            }
    return EvaluationReport(
        dataset_version=dataset.dataset_version,
        repository_id=dataset.repository_id,
        repository_commit=dataset.resolved_commit_sha,
        config_id=config.config_id,
        case_count=len(dataset.cases),
        trial_count=len(runs),
        completed_runs=len(completed),
        failed_runs=len(failures),
        metric_summary=summary,
        information_need_confusion_matrix=confusion,
        failures=failures,
        run_details=tuple(_safe_run_detail(run) for run in runs),
    )
