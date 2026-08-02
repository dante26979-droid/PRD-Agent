from __future__ import annotations

import hashlib

import pytest

from agent.unit import (
    ConfirmedUnitContext,
    OutlineCandidate,
    OutlineNode,
    OutlineUnit,
    RunPurpose,
    UnitScope,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_outline_candidate_hash_is_stable_and_round_trips() -> None:
    candidate = OutlineCandidate(
        title="审批能力 PRD",
        requirement_size="MEDIUM",
        nodes=(
            OutlineNode("goal", "需求目标", 10, "goal"),
            OutlineNode(
                "acceptance",
                "验收标准",
                20,
                "acceptance",
                required_content=("precondition", "trigger", "expected_result"),
            ),
        ),
        units=(
            OutlineUnit("goal", "需求目标", 10, ("goal",)),
            OutlineUnit("acceptance", "验收标准", 20, ("acceptance",), ("goal",)),
        ),
    )

    restored = OutlineCandidate.from_dict(candidate.as_dict())

    assert restored == candidate
    assert len(candidate.content_hash) == 64


@pytest.mark.parametrize(
    "nodes,units,error",
    [
        (
            (OutlineNode("one", "One", 1, "u", parent_key="missing"),),
            (OutlineUnit("u", "U", 1, ("one",)),),
            "missing parent",
        ),
        (
            (
                OutlineNode("one", "One", 1, "u"),
                OutlineNode("two", "Two", 2, "u", parent_key="one"),
                OutlineNode("three", "Three", 3, "u", parent_key="two"),
                OutlineNode("four", "Four", 4, "u", parent_key="three"),
            ),
            (OutlineUnit("u", "U", 1, ("one", "two", "three", "four")),),
            "depth",
        ),
        (
            (OutlineNode("one", "One", 1, "a"), OutlineNode("two", "Two", 2, "b")),
            (
                OutlineUnit("a", "A", 1, ("one",), ("b",)),
                OutlineUnit("b", "B", 2, ("two",), ("a",)),
            ),
            "cycle",
        ),
    ],
)
def test_outline_rejects_invalid_tree_or_dependency_graph(nodes, units, error) -> None:
    with pytest.raises(ValueError, match=error):
        OutlineCandidate(title="x", requirement_size="HIGH", nodes=nodes, units=units)


def test_generation_scope_requires_confirmed_dependency_context() -> None:
    with pytest.raises(ValueError, match="dependencies"):
        UnitScope(
            purpose=RunPurpose.GENERATE_UNIT,
            outline_id="outline-1",
            outline_version=1,
            outline_hash=_hash("outline"),
            current_unit_key="acceptance",
            current_unit_title="验收标准",
            current_unit_ordinal=20,
            section_node_keys=("acceptance",),
            dependency_unit_keys=("solution",),
        )


def test_confirmed_context_validates_markdown_hash() -> None:
    with pytest.raises(ValueError, match="markdown hash"):
        ConfirmedUnitContext(
            unit_key="goal",
            unit_version=1,
            content_hash=_hash("different"),
            markdown="# Goal",
        )


def test_revision_scope_requires_reopened_current_unit_and_disjoint_immutable_set() -> None:
    with pytest.raises(ValueError, match="reopened"):
        UnitScope(
            purpose=RunPurpose.REVISE_UNIT,
            outline_id="outline-1",
            outline_version=1,
            outline_hash=_hash("outline"),
            current_unit_key="goal",
            current_unit_title="需求目标",
            current_unit_ordinal=10,
            section_node_keys=("goal",),
            immutable_unit_keys=("goal",),
            user_feedback="补充指标",
        )
