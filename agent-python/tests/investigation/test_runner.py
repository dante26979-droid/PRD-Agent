from __future__ import annotations

import hashlib

from agent.investigation import (
    CoverageStatus,
    InvestigationBudget,
    InvestigationMode,
    InvestigationRequest,
    InvestigationRunner,
    ProposedAction,
)
from agent.knowledge import EvidenceKnowledgeModule, SourceAuthority
from agent.v1 import agent_execution_pb2 as proto


def test_empty_result_replans_to_a_new_action_and_completes_coverage() -> None:
    selections = []
    executions = []

    def select(context):
        selections.append(context)
        query = "order route" if context.mode is InvestigationMode.INITIAL else "order_route"
        return ProposedAction(
            tool_id="search_repository",
            arguments={"query": query},
            purpose="定位订单路由",
            target_coverage=("repository_structure",),
        )

    def execute(action):
        executions.append(action)
        if action.arguments["query"] == "order route":
            return ()
        excerpt = "def order_route(): ..."
        return (
            proto.EvidenceItem(
                source_type="github",
                source_id="binding-1",
                locator="github://binding-1@" + "a" * 40 + "/orders.py#L1",
                excerpt_hash="sha256:"
                + hashlib.sha256(excerpt.encode()).hexdigest(),
                excerpt=excerpt,
            ),
        )

    result = InvestigationRunner(
        select_action=select,
        execute_action=execute,
        knowledge_module=EvidenceKnowledgeModule(),
    ).run(
        InvestigationRequest(
            run_id="run-1",
            task_id="task-1",
            mode=InvestigationMode.INITIAL,
            need_plan_id="need-1",
            need_context_hash="sha256:" + "1" * 64,
            coverage={"repository_structure": CoverageStatus.MISSING.value},
            source_authorities=(
                SourceAuthority("github", "binding-1", "binding-1", "a" * 40),
            ),
            budget=InvestigationBudget(
                max_iterations=3,
                max_tool_calls=3,
                max_replans=1,
            ),
        )
    )

    assert [context.mode for context in selections] == [
        InvestigationMode.INITIAL,
        InvestigationMode.REPLAN,
    ]
    assert len(executions) == 2
    assert result.coverage == {"repository_structure": "COVERED"}
    assert result.action_history[0].signature != result.action_history[1].signature
    assert result.replan_count == 1


def test_validated_action_resume_does_not_select_a_new_action() -> None:
    action = ProposedAction(
        tool_id="search_repository",
        arguments={"query": "order route"},
        purpose="定位订单路由",
        target_coverage=("repository_structure",),
    )
    excerpt = "def order_route(): ..."
    executions = []

    result = InvestigationRunner(
        select_action=lambda _context: (_ for _ in ()).throw(
            AssertionError("validated action resume must not select again")
        ),
        execute_action=lambda selected: executions.append(selected)
        or (
            proto.EvidenceItem(
                source_type="github",
                source_id="binding-1",
                locator="github://binding-1@" + "a" * 40 + "/orders.py#L1",
                excerpt_hash="sha256:"
                + hashlib.sha256(excerpt.encode()).hexdigest(),
                excerpt=excerpt,
            ),
        ),
        knowledge_module=EvidenceKnowledgeModule(),
    ).run(
        InvestigationRequest(
            run_id="run-1",
            task_id="task-1",
            mode=InvestigationMode.INITIAL,
            need_plan_id="need-1",
            need_context_hash="sha256:" + "1" * 64,
            coverage={"repository_structure": CoverageStatus.MISSING.value},
            source_authorities=(
                SourceAuthority("github", "binding-1", "binding-1", "a" * 40),
            ),
            budget=InvestigationBudget(),
            pending_action=action,
        )
    )

    assert executions == [action]
    assert result.coverage["repository_structure"] == "COVERED"
