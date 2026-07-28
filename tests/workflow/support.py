from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping

from prd_agent.workflow.model import StructuredModelResult


class ScriptedModel:
    def __init__(self, responses: Mapping[str, list[Mapping[str, Any]]]) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls: dict[str, int] = defaultdict(int)

    def complete(self, operation, payload, *, repair=False):
        self.calls[operation] += 1
        values = self.responses.get(operation, [])
        if not values:
            raise AssertionError(f"unexpected model operation: {operation}")
        value = values.pop(0)
        return StructuredModelResult(
            model_id="scripted-model",
            prompt_version=f"{operation}.v1",
            structured_output=value,
            raw_output_hash="sha256:test",
            token_usage={"input": 1, "output": 1},
            finish_reason="stop",
        )


def sufficient_brief() -> dict[str, Any]:
    return {
        "problem": "订单列表缺少按创建时间缩小结果范围的能力",
        "target_users": ["订单运营"],
        "scenarios": ["运营查询指定日期范围内的订单"],
        "goals": ["支持创建时间筛选"],
        "scope_in": ["订单列表"],
        "scope_out": ["历史订单数据迁移"],
        "product_rules": ["筛选条件为开始时间和结束时间"],
        "success_metrics": ["可返回时间范围内的订单"],
        "current_state_fact_ids": [],
        "target_decisions": ["增加创建时间筛选"],
        "authorized_assumptions": [],
        "agent_suggestions": [],
        "open_questions": [],
        "unknown_item_ids": [],
        "source_conflict_ids": [],
    }


def first_outline() -> dict[str, Any]:
    return {
        "title": "订单创建时间筛选",
        "nodes": [
            {
                "title": "筛选规则与验收",
                "purpose": "定义创建时间筛选的交互、规则和验收标准",
                "complexity": "LOW",
                "required_information": [],
            }
        ],
    }

