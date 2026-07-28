from __future__ import annotations

from typing import Any, TypedDict


class PrdWorkflowState(TypedDict, total=False):
    task_id: str
    run_id: str
    thread_id: str
    graph_version: str
    task_version: int
    current_node: str
    user_message: str
    requirement_brief: dict[str, Any]
    outline_id: str
    outline_version: int
    unit_id: str
    generated_content: str
    interrupt_reason: str
    error_code: str
    route: str

