from __future__ import annotations

from .models import NeedPlanningContext


PROMPT_VERSION = "information-need-planner.v1"
SYSTEM_PROMPT = (
    "You plan whether a PRD task needs external investigation. Return exactly one "
    "JSON object with question, suggested_requiredness, need_kind, source_types, "
    "and fallback. Never include credentials or hidden reasoning."
)


def planning_payload(context: NeedPlanningContext) -> dict[str, object]:
    return {
        "task_id": context.task_id,
        "task_version": context.task_version,
        "task_message": context.task_message,
        "workflow_version": context.workflow_version,
        "repository_available": context.code_available,
        "historical_prd_available": context.historical_prd_available,
        "revision_scope": context.revision_scope,
        "allowed_requiredness": ["NONE", "OPTIONAL", "REQUIRED"],
        "allowed_need_kinds": [
            "NEW_BEHAVIOR",
            "FIELD_OR_FORMAT_CHANGE",
            "STATE_OR_RULE_CHANGE",
            "PERMISSION_CHANGE",
            "CODE_LOCATION_ONLY",
            "HISTORICAL_PRD_CONTEXT",
            "CROSS_MODULE_CHANGE",
            "UNKNOWN",
        ],
        "allowed_source_types": ["CODE", "HISTORICAL_PRD", "USER_CONTEXT"],
    }
