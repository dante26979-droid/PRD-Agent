from __future__ import annotations

import uuid

from prd_agent.hashing import sha256_json
from prd_agent.investigation.models import (
    InformationNeed,
    ProposedAction,
    Requiredness,
)


class RepositoryStructureSelector:
    """Deterministic safe first action for the local portfolio workflow."""

    last_token_usage = 0

    def select(self, payload, *, repair: bool = False, replan: bool = False):
        gap = payload.get("gap") or "repository_structure"
        return ProposedAction(
            tool_id="repo_tree",
            tool_schema_version="1",
            arguments={"prefix": "", "max_depth": 5},
            purpose="定位与当前 PRD 单元相关的仓库结构",
            target_coverage=(gap,),
        )


class DefaultUnitInvestigationContextProvider:
    """Run a real bounded investigation and return grounded source objects."""

    def __init__(
        self,
        application_service,
        evidence_store,
        *,
        repository_id: str,
        revision: str | None = None,
    ) -> None:
        self.application_service = application_service
        self.evidence_store = evidence_store
        self.repository_id = repository_id
        self.revision = revision

    def __call__(self, *, task, run, outline, unit, brief) -> dict:
        snapshot = self.application_service.runner.evidence_service.resolve_snapshot(
            self.repository_id, self.revision
        )
        need = InformationNeed(
            information_need_id=f"need-{uuid.uuid4().hex}",
            task_id=task.task_id,
            unit_id=unit.unit_id,
            question=f"定位“{unit.title}”涉及的现有仓库结构和可核查来源",
            requiredness=Requiredness.OPTIONAL,
            source_types=("CODE",),
            required_coverage=("repository_structure",),
            trigger_stage="BEFORE_UNIT_GENERATION",
            fallback="保留 Unknown，并仅生成用户已确认的目标方案",
            planner_version="default-unit-investigation.v1",
            context_hash=sha256_json(
                {
                    "task_id": task.task_id,
                    "unit_id": unit.unit_id,
                    "brief": brief.as_dict(),
                    "outline_version": outline.version,
                }
            ),
        )
        investigation = self.application_service.create(
            need,
            repository_id=snapshot.repository_id,
            resolved_commit_sha=snapshot.resolved_commit_sha,
        )
        if investigation is None:  # pragma: no cover - need is always OPTIONAL
            raise RuntimeError("default investigation was unexpectedly skipped")
        result = self.application_service.run(
            investigation.investigation_id,
            actor_id=task.owner_id,
        )
        executions = self._executions(investigation.investigation_id)
        evidence = tuple(
            item
            for execution in executions
            for item in execution.bundle.evidence
        )
        facts = tuple(
            item for execution in executions for item in execution.bundle.facts
        )
        unknowns = tuple(
            item for execution in executions for item in execution.bundle.unknowns
        )
        conflicts = tuple(
            item for execution in executions for item in execution.bundle.conflicts
        )
        return {
            "investigation_id": investigation.investigation_id,
            "status": result.status.value,
            "stop_reason": result.stop_reason.value,
            "coverage": {
                key: item.status.value for key, item in result.coverage.items()
            },
            "repository_id": snapshot.repository_id,
            "resolved_commit_sha": snapshot.resolved_commit_sha,
            "evidence": evidence,
            "facts": facts,
            "unknowns": unknowns,
            "conflicts": conflicts,
        }

    def _executions(self, investigation_id: str):
        direct = getattr(
            self.evidence_store, "executions_for_investigation", None
        )
        if direct is not None:
            return direct(investigation_id)
        return tuple(
            self.evidence_store.get_execution(call.tool_call_id)
            for call in self.evidence_store.all_calls()
            if call.investigation_id == investigation_id
            and call.status.value
            in {"SUCCEEDED", "PARTIAL", "EMPTY", "FAILED", "BLOCKED"}
        )
