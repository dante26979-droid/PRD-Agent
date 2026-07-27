from __future__ import annotations

from prd_agent.domain.entities import AgentRun
from prd_agent.domain.enums import ACTIVE_RUN_STATUSES, RunStatus
from prd_agent.domain.errors import ActiveRunConflict, InvalidTransition


class RunPolicy:
    def ensure_single_active(self, runs: list[AgentRun], candidate_run_id: str) -> None:
        active = [
            run
            for run in runs
            if run.run_id != candidate_run_id and run.status in ACTIVE_RUN_STATUSES
        ]
        if active:
            raise ActiveRunConflict(f"task already has active run: {active[0].run_id}")

    def ensure_stoppable(self, run: AgentRun) -> None:
        if run.status not in ACTIVE_RUN_STATUSES:
            raise InvalidTransition(f"run in {run.status} cannot be stopped")

    def ensure_retryable(self, run: AgentRun) -> None:
        if run.status not in {RunStatus.FAILED, RunStatus.STOPPED}:
            raise InvalidTransition(f"run in {run.status} cannot be retried")
