from __future__ import annotations

import hashlib

from agent.runtime.idempotency import canonical_json

from .models import (
    InformationNeedPlan,
    NeedBudgetAllocation,
    NeedKind,
    NeedPlanningContext,
    NeedRoute,
    PlannedNeedDecision,
    PlannedNeedDraft,
    Requiredness,
    SourceType,
    CoverageRequirement,
)


_RANK = {
    Requiredness.NONE: 0,
    Requiredness.OPTIONAL: 1,
    Requiredness.REQUIRED: 2,
}

_DESCRIPTIONS = {
    "repository_structure": "相关模块和候选路径已定位",
    "api_contract": "接口字段、类型、必填、枚举或格式已确认",
    "validation_logic": "输入或业务校验位置已确认",
    "storage_schema": "存储字段、类型、空值、默认值和约束已确认",
    "downstream_usage": "下游读取或引用情况已确认",
    "tests": "当前行为和边界测试已确认",
    "authorization_rule": "权限、租户和可见性规则已确认",
    "current_behavior": "当前系统行为已确认",
    "historical_rule_found": "相关历史规则已召回",
    "source_version_identified": "历史文档版本已固定",
    "staleness_assessed": "历史资料时效性已评估",
    "code_conflict_checked": "历史资料与当前代码事实的冲突已检查",
    "decision_conflict_checked": "历史资料与当前目标决策的冲突已检查",
}

_COVERAGE_TEMPLATES = {
    NeedKind.FIELD_OR_FORMAT_CHANGE: (
        "api_contract",
        "validation_logic",
        "storage_schema",
        "tests",
    ),
    NeedKind.STATE_OR_RULE_CHANGE: (
        "validation_logic",
        "downstream_usage",
        "tests",
    ),
    NeedKind.PERMISSION_CHANGE: (
        "authorization_rule",
        "downstream_usage",
        "tests",
    ),
    NeedKind.CODE_LOCATION_ONLY: ("repository_structure",),
    NeedKind.HISTORICAL_PRD_CONTEXT: (
        "historical_rule_found",
        "source_version_identified",
        "staleness_assessed",
        "code_conflict_checked",
        "decision_conflict_checked",
    ),
    NeedKind.CROSS_MODULE_CHANGE: (
        "repository_structure",
        "downstream_usage",
        "tests",
    ),
    NeedKind.UNKNOWN: ("repository_structure", "current_behavior"),
}

_REQUIRED_KINDS = {
    NeedKind.FIELD_OR_FORMAT_CHANGE,
    NeedKind.STATE_OR_RULE_CHANGE,
    NeedKind.PERMISSION_CHANGE,
    NeedKind.CROSS_MODULE_CHANGE,
}


class InformationNeedPolicy:
    policy_version = "information-need-policy.v1"
    planner_version = "information-need-planner.v1"

    def apply(
        self,
        context: NeedPlanningContext,
        draft: PlannedNeedDraft,
    ) -> PlannedNeedDecision:
        context_hash = planning_context_hash(context)
        lower_bound, requiredness_reason = self._lower_bound(context, draft)
        effective = max(
            (draft.suggested_requiredness, lower_bound), key=_RANK.__getitem__
        )
        source_types = self._source_types(draft, effective)
        coverage = self._coverage(draft.need_kind, source_types, effective)
        route, route_reason = self._route(context, effective, source_types, coverage)
        if effective is Requiredness.NONE:
            budget = NeedBudgetAllocation(0, 0, 0, 0)
        else:
            budget = NeedBudgetAllocation(
                max_model_attempts=max(context.remaining_model_attempts, 0),
                max_tool_calls=min(max(context.remaining_tool_calls, 0), 3),
                max_iterations=min(max(context.remaining_iterations, 0), 3),
                max_replans=min(max(context.remaining_replans, 0), 1),
            )
        plan = InformationNeedPlan(
            schema_version="information-need-plan.v1",
            plan_id=f"need-{context_hash[7:31]}",
            context_hash=context_hash,
            question=draft.question.strip(),
            need_kind=draft.need_kind,
            suggested_requiredness=draft.suggested_requiredness,
            effective_requiredness=effective,
            requiredness_reason_code=(
                requiredness_reason
                if _RANK[lower_bound] >= _RANK[draft.suggested_requiredness]
                else "PLANNER_TIGHTENED"
            ),
            source_types=source_types,
            required_coverage=coverage,
            route=route,
            route_reason_code=route_reason,
            fallback=draft.fallback.strip(),
            budget_allocation=budget,
            planner_version=self.planner_version,
            policy_version=self.policy_version,
        )
        return PlannedNeedDecision(plan, route, route_reason)

    @staticmethod
    def _lower_bound(
        context: NeedPlanningContext, draft: PlannedNeedDraft
    ) -> tuple[Requiredness, str]:
        if draft.need_kind in _REQUIRED_KINDS:
            return Requiredness.REQUIRED, "POLICY_REQUIRED_CHANGE_KIND"
        if draft.need_kind is NeedKind.HISTORICAL_PRD_CONTEXT:
            if any(marker in context.task_message for marker in ("沿用", "必须遵循", "历史规则")):
                return Requiredness.REQUIRED, "POLICY_REQUIRED_HISTORICAL_RULE"
            return Requiredness.OPTIONAL, "POLICY_OPTIONAL_HISTORICAL_CONTEXT"
        if draft.need_kind is NeedKind.CODE_LOCATION_ONLY:
            return Requiredness.OPTIONAL, "POLICY_OPTIONAL_CODE_LOCATION"
        if draft.need_kind is NeedKind.UNKNOWN and _has_current_state_signal(
            context.task_message
        ):
            return Requiredness.REQUIRED, "POLICY_REQUIRED_UNKNOWN_CURRENT_CHANGE"
        return Requiredness.NONE, "NONE_NOT_REQUIRED"

    @staticmethod
    def _source_types(
        draft: PlannedNeedDraft, effective: Requiredness
    ) -> tuple[SourceType, ...]:
        if effective is Requiredness.NONE:
            return ()
        if draft.need_kind is NeedKind.HISTORICAL_PRD_CONTEXT:
            required = (SourceType.HISTORICAL_PRD,)
        else:
            required = (SourceType.CODE,)
        return tuple(dict.fromkeys((*required, *draft.source_types)))

    @staticmethod
    def _coverage(
        kind: NeedKind,
        source_types: tuple[SourceType, ...],
        effective: Requiredness,
    ) -> tuple[CoverageRequirement, ...]:
        if effective is Requiredness.NONE:
            return ()
        source = source_types[0]
        return tuple(
            CoverageRequirement(key, source, _DESCRIPTIONS[key], True)
            for key in _COVERAGE_TEMPLATES.get(kind, _COVERAGE_TEMPLATES[NeedKind.UNKNOWN])
        )

    @staticmethod
    def _route(
        context: NeedPlanningContext,
        effective: Requiredness,
        source_types: tuple[SourceType, ...],
        coverage: tuple[CoverageRequirement, ...],
    ) -> tuple[NeedRoute, str]:
        if effective is Requiredness.NONE:
            return NeedRoute.SKIP_INVESTIGATION, "NONE_NOT_REQUIRED"
        source_available = all(
            context.code_available
            if source is SourceType.CODE
            else context.historical_prd_available
            if source is SourceType.HISTORICAL_PRD
            else True
            for source in source_types
        )
        if effective is Requiredness.REQUIRED:
            if not source_available:
                return NeedRoute.PAUSE_FOR_HUMAN, "REQUIRED_SOURCE_UNAVAILABLE"
            if context.remaining_tool_calls < 1 or context.remaining_iterations < 1:
                return NeedRoute.PAUSE_FOR_HUMAN, "REQUIRED_BUDGET_UNAVAILABLE"
            return NeedRoute.EXECUTE_INVESTIGATION, "REQUIRED_SOURCE_AVAILABLE"
        if not source_available:
            return (
                NeedRoute.SKIP_INVESTIGATION,
                "OPTIONAL_SKIPPED_SOURCE_UNAVAILABLE",
            )
        if context.remaining_tool_calls < 1 or context.remaining_iterations < 1:
            return NeedRoute.SKIP_INVESTIGATION, "OPTIONAL_SKIPPED_BUDGET"
        if not coverage:
            return NeedRoute.SKIP_INVESTIGATION, "OPTIONAL_SKIPPED_LOW_VALUE"
        return NeedRoute.EXECUTE_INVESTIGATION, "OPTIONAL_EXECUTED_VALUE"


def planning_context_hash(context: NeedPlanningContext) -> str:
    value = {
        "task_id": context.task_id,
        "task_version": context.task_version,
        "task_message": context.task_message,
        "workflow_version": context.workflow_version,
        "repository_binding_id": context.repository_binding_id,
        "repository_revision": context.repository_revision,
        "revision_scope": context.revision_scope,
        "planner_version": InformationNeedPolicy.planner_version,
        "policy_version": InformationNeedPolicy.policy_version,
    }
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def _has_current_state_signal(value: str) -> bool:
    markers = (
        "修改",
        "变更",
        "字段",
        "接口",
        "API",
        "状态",
        "权限",
        "校验",
        "存储",
        "当前",
        "现有",
    )
    return any(marker in value for marker in markers)
