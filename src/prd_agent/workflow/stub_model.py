"""Deterministic no-network model used by the demo CLI and regression tests."""

from __future__ import annotations

from typing import Any, Mapping

from prd_agent.hashing import sha256_json

from .model import StructuredModelResult


class HeuristicWorkflowModel:
    model_id = "heuristic-workflow-model"

    def complete(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        repair: bool = False,
    ) -> StructuredModelResult:
        output = self._output(operation, payload)
        return StructuredModelResult(
            model_id=self.model_id,
            prompt_version=f"{operation}.m0.step2.v1",
            structured_output=output,
            raw_output_hash=sha256_json(output),
            token_usage={},
            finish_reason="stop",
        )

    def _output(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if operation == "extract_requirement_brief":
            message = str(payload["user_message"]).strip()
            previous = payload.get("previous_brief")
            vague = not previous and any(
                word in message for word in ("优化体验", "优化订单体验")
            )
            old = previous or {}
            return {
                "problem": str(old.get("problem")) if previous else message,
                "target_users": list(old.get("target_users", []))
                if previous
                else ([] if vague else ["产品用户"]),
                "scenarios": list(old.get("scenarios", []))
                if previous
                else ([] if vague else [message]),
                "goals": list(old.get("goals", []))
                if previous
                else ([] if vague else [message]),
                "scope_in": list(old.get("scope_in", []))
                if previous
                else ([] if vague else [message]),
                "scope_out": list(old.get("scope_out", []))
                if previous
                else ["未明确纳入本期的相关改造"],
                "product_rules": list(old.get("product_rules", []))
                if previous
                else ["仅实现用户明确描述的目标行为"],
                "success_metrics": list(old.get("success_metrics", []))
                if previous
                else ["目标场景可以完成并通过验收"],
                "current_state_fact_ids": [],
                "target_decisions": [message],
                "authorized_assumptions": [],
                "agent_suggestions": [],
                "open_questions": []
                if previous or not vague
                else ["具体需要优化哪个角色的哪一个订单场景？"],
                "unknown_item_ids": [],
                "source_conflict_ids": [],
            }
        if operation == "generate_outline":
            brief = payload["requirement_brief"]
            problem = str(brief["problem"])
            return {
                "title": problem[:60],
                "nodes": [
                    {
                        "title": "方案、规则与验收",
                        "purpose": "定义本期方案、业务规则、异常边界和验收标准",
                        "complexity": "MEDIUM",
                        "required_information": [],
                    }
                ],
            }
        if operation == "generate_confirmation_unit":
            brief = payload["requirement_brief"]
            goals = "；".join(brief.get("goals", [])) or "待确认"
            rules = (
                "\n".join(f"- {item}" for item in brief.get("product_rules", []))
                or "- 待确认"
            )
            return {
                "content": (
                    f"### 目标\n\n{goals}\n\n"
                    f"### 规则\n\n{rules}\n\n"
                    "### 验收标准\n\n"
                    "- 当用户输入范围内的合法条件并执行目标操作时，"
                    "系统应返回可验证的预期结果。"
                )
            }
        raise ValueError(f"unsupported workflow model operation: {operation}")
