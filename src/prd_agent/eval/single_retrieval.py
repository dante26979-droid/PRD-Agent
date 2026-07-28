"""One fixed repository action before the minimal PRD workflow."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from prd_agent.application.repository_evidence_service import RepositoryEvidenceService
from prd_agent.domain.commands import ConfirmOutline, ConfirmUnit, StartTask
from prd_agent.evidence.deterministic_validator import DeterministicEvidenceValidator
from prd_agent.hashing import sha256_json
from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.storage.memory_evidence import InMemoryEvidenceStore
from prd_agent.tools.default_registry import build_repository_tool_registry
from prd_agent.tools.models import ToolAction
from prd_agent.workflow.service import WorkflowService

from .models import BaselineConfig, BaselineRun, EvalCase


_ACTIONS = {
    "case-001": ("search_text", {"query": "created_at"}),
    "case-002": ("search_text", {"query": "历史 PRD"}),
    "case-003": ("search_text", {"query": "amount"}),
    "case-004": ("search_text", {"query": "currency"}),
    "case-005": ("search_text", {"query": "cancel"}),
    "case-006": ("search_text", {"query": "operator"}),
    "case-007": (
        "parse_database_schema",
        {"path": "db/schema.sql", "table": "orders"},
    ),
    "case-008": ("search_text", {"query": "ORDER_STATES"}),
    "case-009": ("search_text", {"query": "refund"}),
    "case-010": ("search_text", {"query": "amount"}),
}


class SingleRetrievalBaseline:
    config_version = "single-retrieval.v1"

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def input_hash(self, case: EvalCase, config: BaselineConfig) -> str:
        return sha256_json(
            {
                "case": case.prompt_payload(),
                "action": _ACTIONS.get(case.case_id),
                "config_version": self.config_version,
                "prompt_version": config.prompt_version,
                "dataset_version": config.dataset_version,
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
        try:
            tool_id, arguments = _ACTIONS.get(
                case.case_id, ("repo_tree", {"prefix": "", "max_depth": 4})
            )
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
            evidence_service = RepositoryEvidenceService(
                reader,
                build_repository_tool_registry(reader),
                InMemoryEvidenceStore(),
                DeterministicEvidenceValidator(reader),
            )
            action = ToolAction(
                tool_id=tool_id,
                tool_schema_version="1",
                repository_id=case.repository_id,
                resolved_commit_sha=case.resolved_commit_sha,
                arguments=arguments,
                purpose=f"Single Retrieval for {case.case_id}",
            )
            execution = evidence_service.execute(
                action,
                actor_id="eval",
                idempotency_key=f"{run_id}-retrieval",
            )
            source_lines = [
                f"- {item.path}:{item.line_start or '-'}-{item.line_end or '-'} {item.excerpt}"
                for item in execution.bundle.evidence
            ]
            fact_lines = [
                f"- {item.subject} {item.predicate} = "
                f"{json.dumps(item.value_json, ensure_ascii=False, sort_keys=True)}"
                for item in execution.bundle.facts
            ]
            unknown_lines = [f"- {item.statement}" for item in execution.bundle.unknowns]
            evidence_context = "\n".join(
                source_lines + fact_lines + unknown_lines
            ) or "- 本次单次检索没有可用来源"

            workflow = WorkflowService(InMemoryWorkflowRepository(), model)
            outline_wait = workflow.start_task(
                StartTask(
                    f"{case.requirement}\n\n已知上下文：{case.context}\n\n"
                    f"单次仓库检索结果：\n{evidence_context}",
                    f"{run_id}-start",
                )
            )
            if not outline_wait.current_outline:
                raise ValueError("single retrieval eval requires an outline")
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
        except Exception as exc:  # noqa: BLE001 - failures are evaluation data
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
        )
