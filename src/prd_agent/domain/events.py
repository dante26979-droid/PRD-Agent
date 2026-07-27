from __future__ import annotations

from enum import StrEnum

from .entities import DomainEvent


class EventType(StrEnum):
    TASK_STARTED = "TaskStarted"
    CLARIFICATION_REQUESTED = "ClarificationRequested"
    OUTLINE_GENERATED = "OutlineGenerated"
    OUTLINE_CONFIRMED = "OutlineConfirmed"
    UNIT_GENERATED = "UnitGenerated"
    UNIT_CONFIRMED = "UnitConfirmed"
    PRD_RENDERED = "PrdRendered"
    RUN_FAILED = "RunFailed"


__all__ = ["DomainEvent", "EventType"]
