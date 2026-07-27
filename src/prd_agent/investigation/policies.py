from __future__ import annotations

from collections.abc import Iterable

from prd_agent.tools.models import ToolStatus

from .models import (
    CoverageItem,
    CoverageStatus,
    InformationNeed,
    Investigation,
    InvestigationStatus,
    Requiredness,
    StopReason,
)


COVERAGE_DESCRIPTIONS = {
    "repository_structure": "相关模块和候选路径已定位",
    "api_contract": "接口字段、类型、必填、枚举或格式已确认",
    "validation_logic": "输入或业务校验位置已确认",
    "storage_schema": "存储字段、类型、空值、默认值和约束已确认",
    "downstream_usage": "下游读取或引用情况已确认",
    "tests": "当前行为和边界测试已确认",
    "historical_rule_found": "相关历史规则已召回",
    "source_version_identified": "历史文档和 Corpus 版本已固定",
    "staleness_assessed": "历史资料时效性已评估",
    "code_conflict_checked": "历史资料与当前代码事实的冲突已检查",
    "decision_conflict_checked": "历史资料与当前目标决策的冲突已检查",
}

COVERAGE_TEMPLATES = {
    "FIELD_OR_FORMAT_CHANGE": (
        "api_contract",
        "validation_logic",
        "storage_schema",
        "tests",
    ),
    "STATE_OR_RULE_CHANGE": ("validation_logic", "downstream_usage", "tests"),
    "PERMISSION_CHANGE": ("validation_logic", "downstream_usage", "tests"),
    "CODE_LOCATION_ONLY": ("repository_structure",),
    "HISTORICAL_PRD_CONTEXT": (
        "historical_rule_found",
        "source_version_identified",
        "staleness_assessed",
        "code_conflict_checked",
        "decision_conflict_checked",
    ),
}


class RequirednessPolicy:
    _required_markers = (
        "字段",
        "接口",
        "状态",
        "权限",
        "校验",
        "存储",
        "当前实现",
        "现有逻辑",
        "历史 PRD",
        "历史方案",
        "沿用历史",
    )

    def decide(self, suggested: Requiredness, context: str) -> Requiredness:
        if any(marker in context for marker in self._required_markers):
            return Requiredness.REQUIRED
        return suggested


class CoverageTemplatePolicy:
    def build(self, keys: Iterable[str]) -> dict[str, CoverageItem]:
        unique = tuple(dict.fromkeys(keys))
        return {
            key: CoverageItem(
                key=key,
                description=COVERAGE_DESCRIPTIONS.get(key, key),
                status=CoverageStatus.MISSING,
            )
            for key in unique
        }

    def for_kind(self, need_kind: str) -> dict[str, CoverageItem]:
        return self.build(COVERAGE_TEMPLATES.get(need_kind, ()))


class CoveragePolicy:
    _covered_tools = {
        "repo_tree": {"repository_structure"},
        "parse_openapi": {"api_contract"},
        "parse_database_schema": {"storage_schema"},
        "find_related_tests": {"tests"},
        "read_file": {
            "repository_structure",
            "validation_logic",
            "downstream_usage",
            "tests",
        },
        "find_symbol": {"repository_structure", "validation_logic"},
        "find_references": {"downstream_usage"},
    }

    def update(self, investigation: Investigation, proposal, execution) -> dict[str, CoverageItem]:
        coverage = dict(investigation.coverage)
        evidence_ids = tuple(item.evidence_id for item in execution.bundle.evidence)
        fact_ids = tuple(item.fact_id for item in execution.bundle.facts)
        unknown_ids = tuple(item.unknown_id for item in execution.bundle.unknowns)
        conflict_ids = tuple(item.conflict_id for item in execution.bundle.conflicts)
        for key in proposal.target_coverage:
            current = coverage.get(key)
            if current is None or current.status == CoverageStatus.NOT_REQUIRED:
                continue
            status = current.status
            if conflict_ids:
                status = CoverageStatus.CONFLICTING
            elif execution.tool_result.status == ToolStatus.PARTIAL and evidence_ids:
                status = CoverageStatus.PARTIAL
            elif execution.tool_result.status == ToolStatus.SUCCEEDED and evidence_ids:
                if key in self._covered_tools.get(proposal.tool_id, set()):
                    status = CoverageStatus.COVERED
                else:
                    status = CoverageStatus.PARTIAL
            elif execution.tool_result.status == ToolStatus.EMPTY:
                status = current.status
            coverage[key] = current.model_copy(
                update={
                    "status": status,
                    "evidence_ids": _merge(current.evidence_ids, evidence_ids),
                    "fact_ids": _merge(current.fact_ids, fact_ids),
                    "unknown_ids": _merge(current.unknown_ids, unknown_ids),
                    "conflict_ids": _merge(current.conflict_ids, conflict_ids),
                    "updated_at_iteration": investigation.iteration_count,
                }
            )
        return coverage

    def complete(self, coverage: dict[str, CoverageItem]) -> bool:
        return all(
            item.status in {CoverageStatus.COVERED, CoverageStatus.NOT_REQUIRED}
            for item in coverage.values()
        )

    def next_gap(self, coverage: dict[str, CoverageItem]) -> str | None:
        order = (
            CoverageStatus.CONFLICTING,
            CoverageStatus.MISSING,
            CoverageStatus.PARTIAL,
        )
        for status in order:
            for key, item in coverage.items():
                if item.status == status:
                    return key
        return None


class RoutePolicy:
    def pre_action_stop(self, investigation: Investigation) -> StopReason | None:
        if investigation.token_usage >= investigation.budget.token_budget:
            return StopReason.TOKEN_BUDGET_EXHAUSTED
        if investigation.tool_call_count >= investigation.budget.max_tool_calls:
            return StopReason.TOOL_BUDGET_EXHAUSTED
        if investigation.iteration_count >= investigation.budget.max_iterations:
            return StopReason.MAX_ITERATIONS_REACHED
        return None

    def terminal_status(
        self, investigation: Investigation, reason: StopReason
    ) -> InvestigationStatus:
        if reason == StopReason.COVERAGE_COMPLETE:
            return InvestigationStatus.COMPLETE
        if reason == StopReason.USER_STOPPED:
            return InvestigationStatus.CANCELLED
        if reason in {StopReason.PERMISSION_DENIED, StopReason.HUMAN_INPUT_REQUIRED}:
            return InvestigationStatus.HUMAN_INPUT_REQUIRED
        if reason == StopReason.UNRECOVERABLE_ERROR:
            return InvestigationStatus.FAILED
        if investigation.evidence_ids or investigation.fact_ids:
            return InvestigationStatus.PARTIAL
        return InvestigationStatus.EMPTY


def progress_fingerprint(investigation: Investigation) -> frozenset[str]:
    values = set()
    values.update(f"e:{item}" for item in investigation.evidence_ids)
    values.update(f"f:{item}" for item in investigation.fact_ids)
    values.update(f"u:{item}" for item in investigation.unknown_ids)
    values.update(f"c:{item}" for item in investigation.conflict_ids)
    values.update(
        f"coverage:{key}:{item.status.value}" for key, item in investigation.coverage.items()
    )
    return frozenset(values)


def _merge(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*left, *right)))
