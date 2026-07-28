from __future__ import annotations

from prd_agent.domain.entities import OutlineVersion
from prd_agent.domain.enums import OutlineStatus
from prd_agent.domain.errors import InvalidTransition


class OutlinePolicy:
    def confirm(self, outline: OutlineVersion, requested_version: int) -> None:
        if outline.version != requested_version:
            raise InvalidTransition(
                f"requested outline version {requested_version} is not current version {outline.version}"
            )
        if outline.status != OutlineStatus.PENDING_CONFIRMATION:
            raise InvalidTransition("only a pending outline can be confirmed")
        if not outline.nodes:
            raise InvalidTransition("an outline requires at least one node")
        if not outline.confirmation_units:
            raise InvalidTransition("an outline requires at least one confirmation unit")
        if len(outline.confirmation_units) > 15:
            raise InvalidTransition("an outline cannot exceed 15 confirmation units")
