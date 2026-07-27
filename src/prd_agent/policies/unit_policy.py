from __future__ import annotations

from prd_agent.domain.entities import ConfirmationUnit, OutlineVersion
from prd_agent.domain.enums import OutlineStatus, UnitStatus
from prd_agent.domain.errors import InvalidTransition


class UnitPolicy:
    def confirm(self, outline: OutlineVersion, unit_id: str, current_sequence: int | None) -> None:
        if outline.status != OutlineStatus.CONFIRMED:
            raise InvalidTransition("unit cannot be confirmed before its outline")
        matches = [unit for unit in outline.confirmation_units if unit.unit_id == unit_id]
        if not matches:
            raise InvalidTransition(f"unit does not belong to current outline: {unit_id}")
        unit: ConfirmationUnit = matches[0]
        if unit.sequence != current_sequence:
            raise InvalidTransition("only the current confirmation unit can be confirmed")
        confirmed_ids = {
            item.unit_id
            for item in outline.confirmation_units
            if item.status == UnitStatus.CONFIRMED
        }
        if any(item not in confirmed_ids for item in unit.depends_on_unit_ids):
            raise InvalidTransition("unit dependencies must be confirmed first")
        if unit.status != UnitStatus.PENDING_CONFIRMATION or not unit.content:
            raise InvalidTransition("only generated content pending confirmation can be confirmed")
