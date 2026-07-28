"""Deterministic bounded Investigation Loop evaluation configuration."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import time

from prd_agent.application.investigation_service import InvestigationApplicationService
from prd_agent.application.repository_evidence_service import RepositoryEvidenceService
from prd_agent.domain.commands import ConfirmOutline, ConfirmUnit, StartTask
from prd_agent.evidence.deterministic_validator import DeterministicEvidenceValidator
from prd_agent.hashing import sha256_json
from prd_agent.investigation.models import (
    InformationNeed,
    InvestigationBudget,
    ProposedAction,
)
from prd_agent.investigation.planner import ScriptedActionSelector
from prd_agent.investigation.runner import InvestigationRunner
from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.storage.memory_evidence import InMemoryEvidenceStore
from prd_agent.storage.memory_investigation import InMemoryInvestigationStore
from prd_agent.tools.default_registry import build_repository_tool_registry
from prd_agent.workflow.service import WorkflowService

from .models import BaselineConfig, BaselineRun, EvalCase


def _search_read(query: str, path: str, line_end: int):
    return (
        ProposedAction(
            tool_id="search_text",
            arguments={"query": query},
            purpose=f"定位 {query} 的实现位置",
            target_coverage=("validation_logic",),
        ),
        ProposedAction(
            tool_id="read_file",
            arguments={"path": path, "line_start": 1, "line_end": line_end},
            purpose=f"读取 {query} 的最小实现片段",
            target_coverage=("validation_logic",),
        ),
    )


_PLANS = {
    "case-001": _search_read("created_at", "db/schema.sql", 7),
    "case-002": (
        ProposedAction(
            tool_id="search_text",
            arguments={"query": "历史 PRD"},
            purpose="查找仓库中的历史 PRD 线索",
            target_coverage=("validation_logic",),
        ),
    ),
    "case-003": _search_read("amount", "src/api/validators.py", 3),
    "case-004": _search_read("currency", "src/api/validators.py", 5),
    "case-005": _search_read("cancel", "src/domain/order.py", 12),
    "case-006": _search_read("operator", "src/auth/permissions.py", 8),
    "case-007": (
        ProposedAction(
            tool_id="parse_database_schema",
            arguments={"path": "db/schema.sql", "table": "orders"},
            purpose="解析订单表金额约束",
            target_coverage=("storage_schema",),
        ),
    ),
    "case-008": _search_read("ORDER_STATES", "src/domain/order.py", 1),
    "case-009": _search_read("refund", "src/auth/permissions.py", 8),
    "case-010": _search_read("amount", "src/api/validators.py", 5),
}


class BoundedInvestigationBaseline:
    config_version = "bounded-investigation.v1"

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def input_hash(self, case: EvalCase, config: BaselineConfig) -> str:
        return sha256_json(
            {
                "case": case.prompt_payload(),
                "plan": [item.model_dump(mode="json") for item in _PLANS[case.case_id]],
                "config_version": self.config_version,
                "prompt_version": config.prompt_version,
                "dataset_version": config.dataset_version,
                "budget": dict(config.options),
            }
        )

    def run_id(self, case: EvalCase, config: BaselineConfig, trial_no: int) -> str:
        return "run-" + hashlib.sha256(
            f"{config.config_id}:{case.case_id}:{trial_no}:{self.input_hash(case, config)}".encode()
        ).hexdigest()[:24]

    def run_case(self, case, config, model, *, trial_no: int) -> BaselineRun:
        input_hash = self.input_hash(case, config)
        run_id = self.run_id(case, config, trial_no)
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        metadata = {}
        try:
            reader = GitCliObjectReader(
                RepositoryCatalog(
                    [
                        RepositoryBinding(
                            case.repository_id,
                            self.workspace_root,
                            "eval/fixtures/demo-repo",
                            case.resolved_commit_sha,
                        )
                    ]
                )
            )
            snapshot = reader.resolve_snapshot(case.repository_id, case.resolved_commit_sha)
            evidence_store = InMemoryEvidenceStore()
            evidence_service = RepositoryEvidenceService(
                reader,
                build_repository_tool_registry(reader),
                evidence_store,
                DeterministicEvidenceValidator(reader),
            )
            investigation_store = InMemoryInvestigationStore()
            selector = ScriptedActionSelector(list(_PLANS[case.case_id]))
            runner = InvestigationRunner(
                evidence_service, investigation_store, selector
            )
            application = InvestigationApplicationService(investigation_store, runner)
            coverage_key = "storage_schema" if case.case_id == "case-007" else "validation_logic"
            need = InformationNeed(
                information_need_id=f"need-{run_id}",
                question=case.requirement,
                requiredness="REQUIRED",
                source_types=("CODE",),
                required_coverage=(coverage_key,),
                trigger_stage="EVAL",
                fallback="ASK_USER_OR_MARK_UNKNOWN",
                planner_version="eval-scripted.v1",
                context_hash=sha256_json(case.prompt_payload()),
            )
            investigation = application.create(
                need,
                repository_id=case.repository_id,
                resolved_commit_sha=snapshot.resolved_commit_sha,
                budget=InvestigationBudget(**dict(config.options))
                if config.options
                else None,
            )
            result = application.run(investigation.investigation_id, actor_id="eval")

            source_lines: list[str] = []
            fact_lines: list[str] = []
            unknown_lines: list[str] = []
            for call in evidence_store.all_calls():
                execution = evidence_store.get_execution(call.tool_call_id)
                source_lines.extend(
                    f"- {item.path}:{item.line_start or '-'}-{item.line_end or '-'} {item.excerpt}"
                    for item in execution.bundle.evidence
                )
                fact_lines.extend(
                    f"- {item.subject} {item.predicate} = {item.value_json}"
                    for item in execution.bundle.facts
                )
                unknown_lines.extend(
                    f"- 未知：{item.statement}" for item in execution.bundle.unknowns
                )
            evidence_context = "\n".join(source_lines + fact_lines + unknown_lines)
            evidence_context += (
                f"\n- Investigation: {result.status.value}/{result.stop_reason.value}"
            )

            workflow = WorkflowService(InMemoryWorkflowRepository(), model)
            outline_wait = workflow.start_task(
                StartTask(
                    f"{case.requirement}\n\n已知上下文：{case.context}\n\n"
                    f"受控调查结果：\n{evidence_context}",
                    f"{run_id}-start",
                )
            )
            if not outline_wait.current_outline:
                raise ValueError("bounded investigation eval requires an outline")
            unit_wait = workflow.confirm_outline(
                ConfirmOutline(
                    outline_wait.task.task_id,
                    outline_wait.current_outline.version,
                    outline_wait.task.version,
                    f"{run_id}-outline",
                )
            )
            unit = unit_wait.current_outline.confirmation_units[0]
            final = workflow.confirm_unit(
                ConfirmUnit(
                    unit_wait.task.task_id,
                    unit.unit_id,
                    unit_wait.task.version,
                    f"{run_id}-unit",
                )
            )
            output = final.markdown or ""
            saved = investigation_store.get_investigation(investigation.investigation_id)
            steps = investigation_store.steps(investigation.investigation_id)
            duplicate_actions = sum(
                step.step_type == "VALIDATE_ACTION" and step.status == "BLOCKED"
                for step in steps
            )
            covered = sum(item.status.value == "COVERED" for item in result.coverage.values())
            metadata = {
                "coverage_completion_rate": covered / max(len(result.coverage), 1),
                "tool_call_count": saved.tool_call_count,
                "duplicate_action_rate": duplicate_actions
                / max(saved.tool_call_count + duplicate_actions, 1),
                "replan_count": saved.replan_count,
                "no_progress_termination_rate": float(
                    result.stop_reason.value == "NO_PROGRESS"
                ),
                "stop_reason": result.stop_reason.value,
            }
        except Exception as exc:  # noqa: BLE001
            return BaselineRun(
                eval_run_id=run_id,
                case_id=case.case_id,
                config_id=config.config_id,
                trial_no=trial_no,
                model_id=config.model_id,
                prompt_version=config.prompt_version,
                dataset_version=config.dataset_version,
                repository_commit=case.resolved_commit_sha,
                input_hash=input_hash,
                output_hash=None,
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started) * 1000),
                token_usage={},
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                metadata=metadata,
            )
        return BaselineRun(
            eval_run_id=run_id,
            case_id=case.case_id,
            config_id=config.config_id,
            trial_no=trial_no,
            model_id=getattr(model, "model_id", config.model_id),
            prompt_version=config.prompt_version,
            dataset_version=config.dataset_version,
            repository_commit=case.resolved_commit_sha,
            input_hash=input_hash,
            output_hash="sha256:" + hashlib.sha256(output.encode()).hexdigest(),
            started_at=started_at,
            duration_ms=int((time.perf_counter() - started) * 1000),
            token_usage={},
            status="completed",
            output=output,
            metadata=metadata,
        )
