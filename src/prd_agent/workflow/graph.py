"""Low-level LangGraph definition for the complete workflow execution cursor.

Business writes remain in :class:`WorkflowService`; graph checkpoints only hold
the resumable cursor. Importing this module does not require LangGraph until
``build_graph`` is called, keeping the offline test path lightweight.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .state import PrdWorkflowState


GRAPH_VERSION = "m0.step6.v1"
NODE_NAMES = (
    "LOAD_TASK",
    "EXTRACT_REQUIREMENT_BRIEF",
    "ROUTE_CLARIFICATION",
    "WAIT_USER_CLARIFICATION",
    "GENERATE_OUTLINE",
    "WAIT_OUTLINE_CONFIRMATION",
    "PREPARE_FIRST_UNIT",
    "GENERATE_FIRST_UNIT",
    "WAIT_UNIT_CONFIRMATION",
    "RENDER_MARKDOWN",
    "FINAL_REVIEW",
)


def build_graph(
    handlers: Mapping[str, Callable[[PrdWorkflowState], Mapping[str, Any]]],
    *,
    checkpointer=None,
):
    """Compile the fixed graph using LangGraph's low-level ``StateGraph`` API."""

    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:  # pragma: no cover - optional integration boundary
        raise RuntimeError(
            "LangGraph support requires: python -m pip install '.[workflow]'"
        ) from exc
    missing = [name for name in NODE_NAMES if name not in handlers]
    if missing:
        raise ValueError("missing graph handlers: " + ", ".join(missing))

    graph = StateGraph(PrdWorkflowState)
    for name in NODE_NAMES:
        graph.add_node(name, handlers[name])
    graph.add_edge(START, "LOAD_TASK")
    graph.add_conditional_edges(
        "LOAD_TASK",
        lambda state: state.get("route", "START_TASK"),
        {
            "START_TASK": "EXTRACT_REQUIREMENT_BRIEF",
            "REPLY": "EXTRACT_REQUIREMENT_BRIEF",
            "CONFIRM_OUTLINE": "PREPARE_FIRST_UNIT",
            "CONFIRM_UNIT": "RENDER_MARKDOWN",
        },
    )
    graph.add_edge("EXTRACT_REQUIREMENT_BRIEF", "ROUTE_CLARIFICATION")
    graph.add_conditional_edges(
        "ROUTE_CLARIFICATION",
        lambda state: state["route"],
        {
            "NEED_USER_INPUT": "WAIT_USER_CLARIFICATION",
            "SUFFICIENT": "GENERATE_OUTLINE",
        },
    )
    graph.add_edge("WAIT_USER_CLARIFICATION", END)
    graph.add_edge("GENERATE_OUTLINE", "WAIT_OUTLINE_CONFIRMATION")
    graph.add_edge("WAIT_OUTLINE_CONFIRMATION", END)
    graph.add_edge("PREPARE_FIRST_UNIT", "GENERATE_FIRST_UNIT")
    graph.add_edge("GENERATE_FIRST_UNIT", "WAIT_UNIT_CONFIRMATION")
    graph.add_edge("WAIT_UNIT_CONFIRMATION", END)
    graph.add_edge("RENDER_MARKDOWN", "FINAL_REVIEW")
    graph.add_edge("FINAL_REVIEW", END)
    return graph.compile(checkpointer=checkpointer)
