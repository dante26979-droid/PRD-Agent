"""CLI for validating and running the first M0 baseline."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

from .case_loader import load_dataset
from .models import BaselineConfig, ModelResponse
from .minimal_workflow import MinimalWorkflowBaseline
from .bounded_investigation import BoundedInvestigationBaseline
from .single_retrieval import SingleRetrievalBaseline
from .postgres_store import PostgresRunStore
from .prompt_baseline import DirectPromptBaseline
from .langgraph_runtime import LangGraphRuntimeBaseline
from .report import build_report
from .gates import EvalGateManifest, EvalGateReport, evaluate_gate
from .runner import BaselineRunner, InMemoryRunStore
from prd_agent.workflow.stub_model import HeuristicWorkflowModel


class StubModel:
    model_id = "stub-model"

    def complete(self, system_prompt: str, user_prompt: str, *, timeout_seconds: float):
        return ModelResponse(
            output=(
                "# 需求摘要\n基于固定输入生成的需求摘要。\n\n"
                "## 范围边界\n范围内：需求输入描述的变更。范围外：未提供的信息。\n\n"
                "## 业务规则\n仅实现已知上下文中的规则。\n\n"
                "## 流程\n用户提交后系统校验并返回结果。\n\n"
                "## 验收标准\n当输入满足条件时，系统应返回预期结果。\n\n"
                "## 异常处理\n异常时返回可识别错误。\n\n"
                "## 待确认事项\n信息不足的部分需要用户确认。"
            ),
            model_id=self.model_id,
            token_usage={},
        )


def _load_config(path: Path) -> BaselineConfig:
    value = json.loads(path.read_text(encoding="utf-8"))
    options = dict(value.get("budget", {}))
    for name in ("workflow_version", "fixture_path", "execution_mode"):
        if name in value:
            options[name] = value[name]
    return BaselineConfig(
        config_id=str(value["config_id"]),
        prompt_version=str(value["prompt_version"]),
        model_id=str(value.get("model_id", "stub-model")),
        dataset_version=str(value["dataset_version"]),
        trials_per_case=int(value.get("trials_per_case", 3)),
        timeout_seconds=float(value.get("timeout_seconds", 120)),
        options=options,
    )


def _select_baseline(config: BaselineConfig, workspace_root: Path):
    if config.config_id.startswith("langgraph-runtime-"):
        fixture = config.options.get("fixture_path")
        fixture_path = workspace_root / str(fixture) if fixture else None
        return StubModel(), LangGraphRuntimeBaseline(workspace_root, fixture_path)
    if config.config_id.startswith("bounded-investigation"):
        return HeuristicWorkflowModel(), BoundedInvestigationBaseline(workspace_root)
    if config.config_id.startswith("single-retrieval"):
        return HeuristicWorkflowModel(), SingleRetrievalBaseline(workspace_root)
    if config.config_id.startswith("minimal-workflow"):
        return HeuristicWorkflowModel(), MinimalWorkflowBaseline()
    return StubModel(), DirectPromptBaseline()


def validate_dataset_command(args: argparse.Namespace) -> int:
    dataset = load_dataset(args.manifest)
    print(
        json.dumps(
            {
                "dataset_version": dataset.dataset_version,
                "repository_id": dataset.repository_id,
                "resolved_commit_sha": dataset.resolved_commit_sha,
                "case_count": len(dataset.cases),
            },
            ensure_ascii=False,
        )
    )
    return 0


def run_baseline_command(args: argparse.Namespace) -> int:
    dataset = load_dataset(args.manifest)
    config = _load_config(Path(args.config))
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", config.config_id):
        raise ValueError("baseline config_id is not a safe report filename")
    model, baseline = _select_baseline(config, Path.cwd())
    if args.dsn:
        store = PostgresRunStore.from_dsn(args.dsn)
    else:
        store = InMemoryRunStore()
    runs = BaselineRunner(dataset, config, model, store, baseline=baseline).run()
    report = build_report(dataset, config, runs)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{config.config_id}.md").write_text(report.to_markdown(), encoding="utf-8")
    (output_dir / f"{config.config_id}.json").write_text(report.to_json(), encoding="utf-8")
    print(report.to_markdown())
    return 0 if report.failed_runs == 0 else 1


def evaluate_gate_command(args: argparse.Namespace) -> int:
    manifest_value = json.loads(Path(args.gate).read_text(encoding="utf-8"))
    report_value = json.loads(Path(args.report).read_text(encoding="utf-8"))
    manifest = EvalGateManifest.from_dict(manifest_value)
    report = EvalGateReport.from_dict(report_value)
    decision = evaluate_gate(manifest, report)
    print(json.dumps(decision.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if decision.passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prd-agent-eval")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-dataset")
    validate.add_argument("--manifest", required=True)
    validate.set_defaults(handler=validate_dataset_command)
    run = subparsers.add_parser("run-baseline")
    run.add_argument("--manifest", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--dsn", default=os.environ.get("PRD_AGENT_DATABASE_DSN"))
    run.add_argument("--output-dir", default="eval/reports")
    run.set_defaults(handler=run_baseline_command)
    gate = subparsers.add_parser("evaluate-gate")
    gate.add_argument("--gate", required=True)
    gate.add_argument("--report", required=True)
    gate.set_defaults(handler=evaluate_gate_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
