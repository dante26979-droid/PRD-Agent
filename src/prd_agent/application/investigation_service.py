from __future__ import annotations

import uuid

from prd_agent.hashing import sha256_json

from prd_agent.investigation.models import (
    InformationNeed,
    Investigation,
    InvestigationBudget,
    InvestigationStatus,
    NeedStatus,
    Requiredness,
)
from prd_agent.investigation.policies import CoverageTemplatePolicy


class InvestigationApplicationService:
    def __init__(self, store, runner, planner=None) -> None:
        self.store = store
        self.runner = runner
        self.planner = planner
        self.coverage_templates = CoverageTemplatePolicy()

    def plan_need(self, context, *, trigger_stage: str, **scope) -> InformationNeed:
        if self.planner is None:
            raise RuntimeError("information need planner is not configured")
        need = self.planner.plan(
            context,
            trigger_stage=trigger_stage,
            task_id=scope.get("task_id"),
            run_id=scope.get("run_id"),
            unit_id=scope.get("unit_id"),
        )
        self.store.save_need(need)
        return need

    def create(
        self,
        need: InformationNeed,
        *,
        repository_id: str,
        resolved_commit_sha: str,
        budget: InvestigationBudget | None = None,
    ) -> Investigation | None:
        self.store.save_need(need)
        if need.requiredness == Requiredness.NONE:
            self.store.save_need(need.model_copy(update={"status": NeedStatus.SKIPPED}))
            return None
        if not need.required_coverage:
            raise ValueError("investigation requires at least one coverage item")
        investigation = Investigation(
            investigation_id=f"investigation-{uuid.uuid4().hex}",
            information_need_id=need.information_need_id,
            task_id=need.task_id,
            run_id=need.run_id,
            unit_id=need.unit_id,
            repository_id=repository_id,
            resolved_commit_sha=resolved_commit_sha,
            status=InvestigationStatus.PLANNED,
            coverage=self.coverage_templates.build(need.required_coverage),
            budget=budget or InvestigationBudget(),
            policy_version="investigation.v1+" + sha256_json(need.required_coverage)[7:19],
        )
        self.store.create_investigation(investigation)
        self.store.save_need(need.model_copy(update={"status": NeedStatus.INVESTIGATING}))
        return investigation

    def run(self, investigation_id: str, *, actor_id: str = "local"):
        result = self.runner.run(investigation_id, actor_id=actor_id)
        investigation = self.store.get_investigation(investigation_id)
        need = self.store.get_need(investigation.information_need_id)
        need_status = (
            NeedStatus.SATISFIED
            if result.status == InvestigationStatus.COMPLETE
            else NeedStatus.UNSATISFIED
        )
        self.store.save_need(need.model_copy(update={"status": need_status}))
        return result

    def context(self, investigation_id: str) -> dict:
        """Return the minimal, ID-only context safe for a Workflow model call."""
        investigation = self.store.get_investigation(investigation_id)
        return {
            "investigation_id": investigation.investigation_id,
            "status": investigation.status.value,
            "stop_reason": (
                investigation.stop_reason.value if investigation.stop_reason else None
            ),
            "coverage": {
                key: item.status.value for key, item in investigation.coverage.items()
            },
            "fact_ids": list(investigation.fact_ids),
            "unknown_ids": list(investigation.unknown_ids),
            "conflict_ids": list(investigation.conflict_ids),
            "evidence_ids": list(investigation.evidence_ids),
        }
