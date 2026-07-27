from __future__ import annotations

from copy import deepcopy


class InvestigationVersionConflict(Exception):
    pass


class InMemoryInvestigationStore:
    def __init__(self) -> None:
        self._needs = {}
        self._investigations = {}
        self._steps = {}

    def save_need(self, need) -> None:
        self._needs[need.information_need_id] = deepcopy(need)

    def get_need(self, information_need_id: str):
        return deepcopy(self._needs[information_need_id])

    def create_investigation(self, investigation) -> None:
        if investigation.investigation_id in self._investigations:
            raise InvestigationVersionConflict("investigation already exists")
        for existing in self._investigations.values():
            if (
                existing.information_need_id == investigation.information_need_id
                and existing.status.value in {"PLANNED", "RUNNING"}
            ):
                raise InvestigationVersionConflict(
                    "information need already has an active investigation"
                )
        self._investigations[investigation.investigation_id] = deepcopy(investigation)

    def get_investigation(self, investigation_id: str):
        return deepcopy(self._investigations[investigation_id])

    def save_investigation(self, investigation, *, expected_version: int) -> None:
        current = self._investigations[investigation.investigation_id]
        if current.version != expected_version:
            raise InvestigationVersionConflict(
                f"expected version {expected_version}, current version is {current.version}"
            )
        self._investigations[investigation.investigation_id] = deepcopy(investigation)

    def append_step(self, step) -> None:
        values = self._steps.setdefault(step.investigation_id, [])
        if any(item.sequence == step.sequence for item in values):
            raise InvestigationVersionConflict("step sequence already exists")
        values.append(deepcopy(step))

    def steps(self, investigation_id: str):
        return tuple(deepcopy(self._steps.get(investigation_id, [])))

    def investigations_for_task(self, task_id: str):
        return tuple(
            deepcopy(item)
            for item in self._investigations.values()
            if item.task_id == task_id
        )
