from __future__ import annotations


class InvestigationReadService:
    """Assemble owner-checked public trace input from investigation stores."""

    def __init__(self, investigation_store, evidence_store) -> None:
        self.investigation_store = investigation_store
        self.evidence_store = evidence_store

    def for_task(self, task_id: str):
        result = []
        for investigation in self.investigation_store.investigations_for_task(
            task_id
        ):
            need = self.investigation_store.get_need(
                investigation.information_need_id
            )
            executions = self.evidence_store.executions_for_investigation(
                investigation.investigation_id
            )
            result.append(
                {
                    "investigation": investigation,
                    "need": need,
                    "steps": self.investigation_store.steps(
                        investigation.investigation_id
                    ),
                    "executions": executions,
                }
            )
        return tuple(result)
