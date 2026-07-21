"""Markdown and JSON report generation for baseline runs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from statistics import mean
from typing import Any

from .metrics import MetricResult, evaluate_case
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
            "failures": list(self.failures),
            "run_details": list(self.run_details),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def to_markdown(self) -> str:
        lines = [
            "# M0 Direct Prompt Baseline Report",
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
        lines.extend(["", "## Run Details", "", "| Run | Case | Trial | Status | Input Hash |", "| --- | --- | ---: | --- | --- |"])
        for run in self.run_details:
            lines.append(
                f"| {run['eval_run_id']} | {run['case_id']} | {run['trial_no']} | "
                f"{run['status']} | {run['input_hash']} |"
            )
        if self.failures:
            lines.extend(["", "## Failures", ""])
            for failure in self.failures:
                lines.append(f"- `{failure['eval_run_id']}` ({failure['case_id']}): {failure['error']}")
        return "\n".join(lines) + "\n"


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
    for run in completed:
        case = next(case for case in dataset.cases if case.case_id == run.case_id)
        metric_results.extend(evaluate_case(case, run.output or ""))
    failures = tuple(
        {
            "eval_run_id": run.eval_run_id,
            "case_id": run.case_id,
            "trial_no": run.trial_no,
            "error": run.error or "unknown error",
        }
        for run in runs
        if run.status != "completed"
    )
    return EvaluationReport(
        dataset_version=dataset.dataset_version,
        repository_id=dataset.repository_id,
        repository_commit=dataset.resolved_commit_sha,
        config_id=config.config_id,
        case_count=len(dataset.cases),
        trial_count=len(runs),
        completed_runs=len(completed),
        failed_runs=len(failures),
        metric_summary=_summarize(metric_results),
        failures=failures,
        run_details=tuple(run.as_dict() for run in runs),
    )
